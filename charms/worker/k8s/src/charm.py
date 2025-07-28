#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""K8s Charm.

A machine charm which operates a complete Kubernetes cluster.

This charm installs and operates a Kubernetes cluster via the k8s snap. It exposes
relations to co-operate with other kubernetes components such as optional CNIs,
optional cloud-providers, optional schedulers, external backing stores, and external
certificate storage.
"""

import hashlib
import ipaddress
import logging
import os
import shlex
import socket
import subprocess
from collections import defaultdict
from functools import cached_property
from pathlib import Path
from time import sleep
from typing import Dict, FrozenSet, List, Optional, Tuple, Union
from urllib.parse import urlparse

import config.arg_files
import config.bootstrap
import config.extra_args
import config.resource
import containerd
import k8s.node
import ops
import utils
import yaml
from certificates import EtcdCertificates, K8sCertificates, RefreshCertificates
from charmed_etcd import CharmedEtcdRequires
from cloud_integration import CloudIntegration
from config.cluster import assemble_cluster_config
from cos_integration import COSIntegration
from endpoints import build_url
from events import update_status
from inspector import ClusterInspector
from k8s.client import kubectl
from kube_control import configure as configure_kube_control
from literals import (
    APISERVER_CERT,
    APISERVER_PORT,
    BOOTSTRAP_CERTIFICATES,
    BOOTSTRAP_DATASTORE,
    BOOTSTRAP_NODE_TAINTS,
    BOOTSTRAP_POD_CIDR,
    BOOTSTRAP_SERVICE_CIDR,
    CHARMED_ETCD_RELATION,
    CLUSTER_CERTIFICATES_DOMAIN_NAME_KEY,
    CLUSTER_CERTIFICATES_KEY,
    CLUSTER_CERTIFICATES_KUBELET_FORMATTER_KEY,
    CLUSTER_RELATION,
    CLUSTER_WORKER_RELATION,
    COMMON_NAME_CONFIG_KEY,
    CONTAINERD_HTTP_PROXY,
    CONTAINERD_RELATION,
    CONTAINERD_SERVICE_NAME,
    COS_RELATION,
    COS_TOKENS_RELATION,
    COS_TOKENS_WORKER_RELATION,
    DATASTORE_NAME_MAPPING,
    DATASTORE_TYPE_ETCD,
    DATASTORE_TYPE_EXTERNAL,
    DATASTORE_TYPE_K8S_DQLITE,
    DEPENDENCIES,
    ETC_KUBERNETES,
    ETCD_CERTIFICATES_RELATION,
    ETCD_RELATION,
    EXTERNAL_LOAD_BALANCER_PORT,
    EXTERNAL_LOAD_BALANCER_RELATION,
    EXTERNAL_LOAD_BALANCER_REQUEST_NAME,
    EXTERNAL_LOAD_BALANCER_RESPONSE_NAME,
    K8SD_PORT,
    K8SD_SNAP_SOCKET,
    KUBECONFIG,
    KUBECTL_PATH,
    KUBELET_CN_FORMATTER_CONFIG_KEY,
    SNAP_RESOURCE_NAME,
    SUPPORTED_DATASTORES,
)
from loadbalancer_interface import LBProvider
from ops.interface_kube_control import KubeControlProvides
from pki import get_certificate_sans
from pydantic import SecretStr
from snap import management as snap_management
from snap import version as snap_version
from token_distributor import ClusterTokenType, TokenCollector, TokenDistributor, TokenStrategy
from typing_extensions import Literal
from upgrade import K8sDependenciesModel, K8sUpgrade

import charms.contextual_status as status
import charms.node_base.address as node_address
import charms.operator_libs_linux.v2.snap as snap_lib
from charms.contextual_status import ReconcilerError, on_error
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    CreateClusterRequest,
    InvalidResponseError,
    JoinClusterRequest,
    K8sdAPIManager,
    K8sdConnectionError,
    NodeJoinConfig,
    UnixSocketConnectionFactory,
    UpdateClusterConfigRequest,
    UserFacingDatastoreConfig,
)
from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from charms.node_base import LabelMaker
from charms.operator_libs_linux.v1 import systemd
from charms.reconciler import Reconciler

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


def _get_juju_public_address() -> str:
    """Get public address from juju.

    Returns:
        (str) public ip address of the unit
    """
    cmd = ["unit-get", "public-address"]
    return subprocess.check_output(cmd).decode("UTF-8").strip()


def _cluster_departing_unit(event: ops.EventBase) -> Union[Literal[False], ops.Unit]:
    """Determine if the given event signals the end of the cluster for this unit.

    Args:
        event (ops.EventBase): event to consider.

    Returns:
        Literal[False] | ops.Unit - False or the Unit leaving the cluster
    """
    return (
        isinstance(event, ops.RelationDepartedEvent)
        and event.relation.name in ["k8s-cluster", "cluster"]
        and event.departing_unit
        or False
    )


class NodeRemovedError(Exception):
    """Raised to prevent reconciliation of dying node."""


class K8sCharm(ops.CharmBase):
    """A charm for managing a K8s cluster via the k8s snap.

    Attrs:
        is_worker: true if this is a worker unit
        is_control_plane: true if this is a control-plane unit
        lead_control_plane: true if this is a control-plane unit and its the leader
        is_upgrade_granted: true if the upgrade has been granted
        datastore: the datastore used for Kubernetes
        certificate_refresh: event source for certificate refresh
        external_load_balancer_address: the external load balancer address, if available
    """

    _stored = ops.StoredState()
    certificate_refresh = ops.EventSource(RefreshCertificates)

    def __init__(self, *args):
        """Initialise the K8s charm.

        Args:
            args: Arguments passed to the CharmBase parent constructor.
        """
        super().__init__(*args)
        factory = UnixSocketConnectionFactory(unix_socket=K8SD_SNAP_SOCKET, timeout=320)
        self.snap_installation_resource = config.resource.CharmResource(self, SNAP_RESOURCE_NAME)
        self.api_manager = K8sdAPIManager(factory)
        xcp_relation = "external-cloud-provider" if self.is_control_plane else ""
        self.cloud_integration = CloudIntegration(self, self.is_control_plane)
        self.xcp = ExternalCloudProvider(self, xcp_relation)
        self.cluster_inspector = ClusterInspector(kubeconfig_path=self.kubeconfig)
        self.upgrade = K8sUpgrade(
            self,
            cluster_inspector=self.cluster_inspector,
            relation_name="upgrade",
            substrate="vm",
            dependency_model=K8sDependenciesModel(**DEPENDENCIES),
        )
        self.cos = COSIntegration(self)
        self.update_status = update_status.Handler(self, self.upgrade)
        self.distributor = TokenDistributor(self, self.get_node_name(), self.api_manager)
        self.collector = TokenCollector(self, self.get_node_name())
        self.labeller = LabelMaker(
            self,
            kubeconfig_path=self.kubeconfig,
            kubectl=KUBECTL_PATH,
            user_label_key="node-labels",
            timeout=15,
        )
        self._stored.set_default(is_dying=False, cluster_name=str())

        self.cos_agent = COSAgentProvider(
            self,
            scrape_configs=self._get_scrape_jobs,
            refresh_events=[
                self.on.cluster_relation_joined,
                self.on.cluster_relation_changed,
                self.on.cos_tokens_relation_joined,
                self.on.cos_tokens_relation_changed,
                self.on.config_changed,
                self.on.upgrade_charm,
                self.cos.refresh_event,
            ],
        )

        self.certificates = K8sCertificates(self, self.certificate_refresh)
        custom_events = self.certificates.events
        if self.lead_control_plane:
            self.etcd_certificate = EtcdCertificates(self)
            custom_events += self.etcd_certificate.events

        if self.is_control_plane:
            self.etcd = self._initialize_external_etcd()
            self.kube_control = KubeControlProvides(self, endpoint="kube-control")
            self.framework.observe(self.on.get_kubeconfig_action, self._get_external_kubeconfig)
            self.external_load_balancer = LBProvider(self, EXTERNAL_LOAD_BALANCER_RELATION)

        self.reconciler = Reconciler(
            self,
            self._reconcile,
            exit_status=self.update_status.active_status,
            custom_events=custom_events,
        )
        self.framework.observe(self.on.refresh_certs_action, self._on_refresh_certs_action)

    @property
    def external_load_balancer_address(self) -> str:
        """Return the external load balancer address.

        Raises:
            LookupError: If the loadbalancer response has errors.
        """
        if not self.is_control_plane:
            log.warning("External load balancer is only configured for control-plane units.")
            return ""

        if not self.external_load_balancer.is_available:
            log.warning(
                "External load balancer relation is not available but the address was requested."
            )
            return ""

        resp = self.external_load_balancer.get_response(EXTERNAL_LOAD_BALANCER_RESPONSE_NAME)
        if not resp:
            log.error("No response from external load balancer")
            return ""
        if resp.error:
            raise LookupError(f"External load balancer error: {resp.error}")

        return resp.address

    def _k8s_info(self, event: ops.EventBase):
        """Send cluster information on the kubernetes-info relation.

        Provide applications with cluster characteristics. This should only run on the lead
        k8s control plane unit.

        Args:
            event: ops.RelationChangedEvent - event triggered by the relation changed hook
        """
        if isinstance(event, ops.RelationChangedEvent) and event.relation.name == "ceph-k8s-info":
            event.relation.data[self.app]["kubelet-root-dir"] = "/var/lib/kubelet"

    @status.on_error(
        ops.WaitingStatus("Installing COS requirements"),
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    )
    def _apply_cos_requirements(self):
        """Apply COS requirements for integration.

        This method applies COS requirements for integration. It configures COS
        Integration by applying the manifests for COS Cluster Roles and
        kube-state-metrics (K-S-M).
        """
        if not self.model.relations[COS_RELATION]:
            return

        log.info("Apply COS Integrations")
        status.add(ops.MaintenanceStatus("Ensuring COS Integration"))
        kubectl("apply", "-f", "templates/cos_roles.yaml", kubeconfig=self.kubeconfig)
        kubectl("apply", "-f", "templates/ksm.yaml", kubeconfig=self.kubeconfig)
        self.cos.trigger_jobs_refresh()

    @property
    def is_control_plane(self) -> bool:
        """Returns true if the unit is a control-plane."""
        return not self.is_worker

    @property
    def lead_control_plane(self) -> bool:
        """Returns true if the unit is the leader control-plane."""
        return self.is_control_plane and self.unit.is_leader()

    @cached_property
    def is_worker(self) -> bool:
        """Returns true if the unit is a worker."""
        return self.meta.name == "k8s-worker"

    def get_worker_versions(self) -> Dict[str, List[ops.Unit]]:
        """Get the versions of the worker units.

        Returns:
            Dict[str, List[ops.Unit]]: A dictionary of versions and the units that have them.
        """
        versions = defaultdict(list)
        for relation in self.model.relations[CLUSTER_WORKER_RELATION]:
            for unit in relation.units:
                if version := relation.data[unit].get("version"):
                    versions[version].append(unit)
        return versions

    def _apply_proxy_environment(self):
        """Apply the proxy settings from environment variables."""
        proxy_settings = self._get_proxy_systemd_config()
        if proxy_settings:
            CONTAINERD_HTTP_PROXY.parent.mkdir(parents=True, exist_ok=True)
            existing = (
                CONTAINERD_HTTP_PROXY.exists()
                and CONTAINERD_HTTP_PROXY.read_text(encoding="utf-8")
                or ""
            )
            if written := existing != proxy_settings:
                log.info("Applying Proxied Environment Settings")
                CONTAINERD_HTTP_PROXY.write_text(proxy_settings, encoding="utf-8")
                systemd.daemon_reload()

            if written and systemd.service_running(CONTAINERD_SERVICE_NAME):
                # Reload the containerd service to apply the new settings
                log.info("Restarting %s", CONTAINERD_SERVICE_NAME)
                systemd.service_restart(CONTAINERD_SERVICE_NAME)
            else:
                log.info("No changes to proxy settings, skipping reload")
        else:
            log.info("No proxy settings to apply")

    def _generate_unique_cluster_name(self) -> str:
        """Use a stable input to generate a unique cluster name.

        Returns:
            str: The unique cluster name.
        """
        stable_input = f"{self.app.name}-{self.model.uuid}"
        hashed = hashlib.sha256(stable_input.encode()).hexdigest()[:32]
        return f"k8s-{hashed}"

    def get_cluster_name(self) -> str:
        """Craft a unique name for the cluster once joined or bootstrapped.

        Note: It won't change for the lifetime of the unit.

        Returns:
            the cluster name.
        """
        unit, node = self.unit.name, self.get_node_name()
        if self._stored.cluster_name == "":
            if self.lead_control_plane:
                log.info("Lead control plane node %s generating unique cluster name.", node)
                self._stored.cluster_name = self._generate_unique_cluster_name()
            elif not (relation := self.model.get_relation(CLUSTER_RELATION)):
                log.warning(
                    "Node %s has no '%s' relation, cannot determine cluster name.",
                    node,
                    CLUSTER_RELATION,
                )
            elif (
                result := k8s.node.present(self.kubeconfig, node)
            ) != k8s.node.Presence.AVAILABLE:
                log.warning("Node %s, is not available in cluster (status %s).", node, result)
            elif self.is_worker or self.api_manager.is_cluster_bootstrapped():
                self._stored.cluster_name = self.collector.cluster_name(relation, True)
            else:
                log.warning("Node %s isn't bootstrapped, skipping cluster name.", node)

        log.info("%s(%s) current cluster-name=%s", unit, node, self._stored.cluster_name)
        return str(self._stored.cluster_name)

    def get_node_name(self) -> str:
        """Return the lowercase hostname.

        Returns:
            the hostname of the machine.
        """
        if self.xcp.name == "aws":
            return socket.getfqdn().lower()
        return socket.gethostname().lower()

    def get_cloud_name(self) -> str:
        """Return the underlying cloud name.

        Returns:
            the cloud hosting the machine.
        """
        return self.xcp.name or ""

    @on_error(ops.BlockedStatus("Failed to install snaps."), snap_lib.SnapError)
    def _install_snaps(self):
        """Install snap packages."""
        status.add(ops.MaintenanceStatus("Ensuring snap installation"))
        snap_management(self)

    @on_error(
        ops.WaitingStatus("Waiting to apply snap requirements"), subprocess.CalledProcessError
    )
    def _apply_snap_requirements(self):
        """Apply necessary snap requirements for the k8s snap.

        This method executes necessary scripts to ensure that the snap
        meets the network and interface requirements.
        """
        status.add(ops.MaintenanceStatus("Ensuring snap requirements"))
        log.info("Applying K8s requirements")
        init_sh = "/snap/k8s/current/k8s/hack/init.sh"
        subprocess.check_call(shlex.split(init_sh))

    @on_error(ops.WaitingStatus("Waiting for k8sd"), InvalidResponseError, K8sdConnectionError)
    def _check_k8sd_ready(self):
        """Check if k8sd is ready to accept requests."""
        log.info("Check if k8ds is ready")
        status.add(ops.MaintenanceStatus("Ensuring snap readiness"))
        self.api_manager.check_k8sd_ready()

    def split_sans_by_type(self) -> Tuple[FrozenSet[str], FrozenSet[str]]:
        """Split SANs into IP addresses and DNS names.

        Returns:
            Tuple[FrozenSet[str], FrozenSet[str]]: IP addresses and DNS names.
        """
        ip_sans = set()
        dns_sans = set()

        for san in self._get_extra_sans():
            try:
                ip = ipaddress.ip_address(san)
                ip_sans.add(str(ip))
            except ValueError:
                dns_sans.add(san)
        return frozenset(ip_sans), frozenset(dns_sans)

    def _get_node_ips(self) -> List[str]:
        """Get the cluster node addresses for this unit.

        Returns:
            list[str]: A list containing up to two IP addresses for each IP
                version.
        """
        return node_address.by_relation_preferred(self, CLUSTER_RELATION, True)

    def _get_extra_sans(self):
        """Retrieve the certificate extra SANs.

        Raises:
            ReconcilerError: If it fails to get the external load balancer address.
        """
        # Get the extra SANs from the configuration
        extra_sans_str = str(self.config.get("kube-apiserver-extra-sans") or "")
        extra_sans = set(extra_sans_str.strip().split())

        # Add the ingress addresses of all units
        extra_sans.add(_get_juju_public_address())
        if addresses := node_address.by_relation(self, CLUSTER_RELATION, True):
            log.info("Adding ingress addresses to extra SANs")
            extra_sans |= set(addresses)

        # Add the external load balancer address
        try:
            if lb_addr := self.external_load_balancer_address:
                log.info("Adding external load balancer address to extra SANs")
                extra_sans.add(lb_addr)
        except LookupError as e:
            raise ReconcilerError(f"Failed to get external load balancer address: {e}") from e

        return sorted(extra_sans)

    def _assemble_bootstrap_config(self):
        """Assemble the bootstrap configuration for the Kubernetes cluster.

        Returns:
            BootstrapConfig: The bootstrap configuration object.
        """
        bootstrap_config = BootstrapConfig.model_construct()
        self.certificates.configure_certificates(bootstrap_config)
        self._configure_datastore(bootstrap_config)
        bootstrap_config.cluster_config = assemble_cluster_config(
            self, "external" if self.xcp.has_xcp else None
        )
        bootstrap_config.service_cidr = BOOTSTRAP_SERVICE_CIDR.get(self)
        bootstrap_config.pod_cidr = BOOTSTRAP_POD_CIDR.get(self)
        bootstrap_config.control_plane_taints = BOOTSTRAP_NODE_TAINTS.get(self).split()
        bootstrap_config.extra_sans = self._get_extra_sans()
        cluster_name = self.get_cluster_name()
        node_ips = self._get_node_ips()
        config.extra_args.craft(self.config, bootstrap_config, cluster_name, node_ips)
        return bootstrap_config

    def _configure_external_load_balancer(self) -> None:
        """Configure the external load balancer for the application.

        This method checks if the external load balancer is available and then
        proceeds to configure it by sending a request with the necessary parameters.
        It waits for a response from the external load balancer and handles any errors that
        may occur during the process.
        """
        if not self.is_control_plane:
            log.info("External load balancer is only configured for control-plane units.")
            return

        if not self.external_load_balancer.is_available:
            log.info("External load balancer relation is not available. Skipping setup.")
            return

        status.add(ops.MaintenanceStatus("Configuring external loadBalancer"))

        req = self.external_load_balancer.get_request(EXTERNAL_LOAD_BALANCER_REQUEST_NAME)
        req.protocol = req.protocols.tcp
        req.port_mapping = {EXTERNAL_LOAD_BALANCER_PORT: APISERVER_PORT}
        req.public = True
        if not req.health_checks:
            req.add_health_check(protocol=req.protocols.https, port=APISERVER_PORT, path="/livez")
        self.external_load_balancer.send_request(req)
        log.info("External load balancer request was sent")

        resp = self.external_load_balancer.get_response(EXTERNAL_LOAD_BALANCER_RESPONSE_NAME)
        if not resp:
            msg = "No response from external load balancer"
            status.add(ops.WaitingStatus(msg))
            raise ReconcilerError(msg)
        if resp.error:
            msg = f"External load balancer error: {resp.error}"
            status.add(ops.BlockedStatus(msg))
            raise ReconcilerError(msg)

        log.info("External load balancer is configured with address %s", resp.address)

    @on_error(
        ops.WaitingStatus("Waiting to bootstrap k8s snap"),
        ReconcilerError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _bootstrap_k8s_snap(self):
        """Bootstrap k8s if it's not already bootstrapped."""
        if self.api_manager.is_cluster_bootstrapped():
            log.info("K8s cluster already bootstrapped")
            return

        if not (node_ips := self._get_node_ips()):
            log.info("Cannot cluster yet, no node IPs found")
            raise ReconcilerError("No node IPs found")

        status.add(ops.MaintenanceStatus("Bootstrapping Cluster"))
        payload = CreateClusterRequest(
            name=self.get_node_name(),
            address=f"{node_ips[0]}:{K8SD_PORT}",
            config=self._assemble_bootstrap_config(),
        )

        # TODO: Make port (and address) configurable.
        self.api_manager.bootstrap_k8s_snap(payload)

    @on_error(
        ops.BlockedStatus("Failed to apply containerd_custom_registries, check logs for details"),
        ValueError,
        FileNotFoundError,
        OSError,
    )
    def _config_containerd_registries(self):
        """Apply containerd custom registries."""
        registries, config = [], ""
        if self.is_control_plane:
            config = str(self.config["containerd-custom-registries"])
            registries = containerd.parse_registries(config)
            containerd.ensure_registry_configs(registries)

        for relation in self.model.relations[CONTAINERD_RELATION]:
            if self.lead_control_plane:
                containerd.share(config, self.app, relation)
                continue
            if self.is_control_plane:
                continue
            # Only workers here, and they are limited to only relate to one containerd endpoint
            self.unit.status = ops.MaintenanceStatus("Ensuring containerd registries")
            registries = containerd.recover(relation)
            containerd.ensure_registry_configs(registries)

    def _configure_cos_integration(self):
        """Retrieve the join token from secret databag and join the cluster."""
        if not self.model.relations[COS_RELATION]:
            return

        status.add(ops.MaintenanceStatus("Updating COS integrations"))
        log.info("Updating COS integration")
        if relation := self.model.get_relation(COS_TOKENS_RELATION):
            self.collector.request(relation)

    def _get_valid_annotations(self) -> Optional[dict]:
        """Fetch and validate cluster-annotations from charm configuration.

        The values are expected to be a space-separated string of key-value pairs.

        Returns:
            dict: The parsed annotations if valid, otherwise None.

        Raises:
            ReconcilerError: If any annotation is invalid.
        """
        raw_annotations = self.config.get("cluster-annotations")
        if not raw_annotations:
            return None

        raw_annotations = str(raw_annotations)

        annotations = {}
        try:
            for key, value in [pair.split("=", 1) for pair in raw_annotations.split()]:
                if not key or not value:
                    raise ReconcilerError("Invalid Annotation")
                annotations[key] = value
        except ReconcilerError:
            log.exception("Invalid annotations: %s", raw_annotations)
            status.add(ops.BlockedStatus("Invalid Annotations"))
            raise

        return annotations

    def _configure_datastore(self, config: Union[BootstrapConfig, UpdateClusterConfigRequest]):
        """Configure the datastore for the Kubernetes cluster.

        Args:
            config (BootstrapConfig|UpdateClusterConfigRequst):
                The configuration object for the Kubernetes cluster. This object
                will be modified in-place to include etcd's configuration details.
        """
        datastore = BOOTSTRAP_DATASTORE.get(self)

        if datastore not in SUPPORTED_DATASTORES:
            log.error(
                "Invalid datastore: %s. Supported values: %s",
                datastore,
                ", ".join(SUPPORTED_DATASTORES),
            )
            status.add(ops.BlockedStatus(f"Invalid datastore: {datastore}"))
            raise ReconcilerError(f"Invalid datastore: {datastore}")

        if datastore == DATASTORE_TYPE_EXTERNAL:
            log.info("Using etcd as external datastore")

            if not self.etcd:
                raise ReconcilerError("Missing etcd relation")

            self.etcd.update_relation_data()

            if not self.etcd.is_ready:
                status.add(ops.WaitingStatus("Waiting for etcd to be ready"))
                raise ReconcilerError("etcd is not ready")

            etcd_config = self.etcd.get_client_credentials()
            if isinstance(config, BootstrapConfig):
                config.datastore_type = DATASTORE_NAME_MAPPING.get(datastore)
                config.datastore_servers = self.etcd.get_connection_string().split(",")
                config.datastore_ca_cert = etcd_config.get("client_ca", "")
                config.datastore_client_cert = etcd_config.get("client_cert", "")
                config.datastore_client_key = etcd_config.get("client_key", "")
                log.info("etcd servers: %s", config.datastore_servers)
            elif isinstance(config, UpdateClusterConfigRequest):
                config.datastore = UserFacingDatastoreConfig()
                config.datastore.type = DATASTORE_NAME_MAPPING.get(datastore)
                config.datastore.servers = self.etcd.get_connection_string().split(",")
                config.datastore.ca_crt = etcd_config.get("client_ca", "")
                config.datastore.client_crt = etcd_config.get("client_cert", "")
                config.datastore.client_key = etcd_config.get("client_key", "")
                log.info("etcd servers: %s", config.datastore.servers)

        elif datastore == DATASTORE_TYPE_ETCD and isinstance(config, BootstrapConfig):
            config.datastore_type = DATASTORE_NAME_MAPPING.get(DATASTORE_TYPE_ETCD)
            log.info("Using managed etcd as datastore")
        elif datastore == DATASTORE_TYPE_K8S_DQLITE and isinstance(config, BootstrapConfig):
            config.datastore_type = DATASTORE_NAME_MAPPING.get(DATASTORE_TYPE_K8S_DQLITE)
            log.info("Using dqlite as datastore")

    def _revoke_cluster_tokens(self, event: ops.EventBase):
        """Revoke tokens for the units in the cluster and k8s-cluster relations.

        if self is dying, only try to remove itself from the cluster
        if event is relation_departed, remove that unit

        Args:
            event (ops.Event): event triggering token revocation

        """
        log.info("Garbage collect cluster tokens")
        to_remove = None
        if self._stored.is_dying is True:
            to_remove = self.unit
        elif unit := _cluster_departing_unit(event):
            to_remove = unit

        if peer := self.model.get_relation(CLUSTER_RELATION):
            self.distributor.revoke_tokens(
                relation=peer,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.CONTROL_PLANE,
                to_remove=to_remove,
            )

        for relation in self.model.relations[CLUSTER_WORKER_RELATION]:
            self.distributor.revoke_tokens(
                relation=relation,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.WORKER,
                to_remove=to_remove,
            )

    def _create_cluster_tokens(self):
        """Create tokens for the units in the cluster and k8s-cluster relations."""
        log.info("Prepare clustering")
        if peer := self.model.get_relation(CLUSTER_RELATION):
            self.distributor.allocate_tokens(
                relation=peer,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.CONTROL_PLANE,
            )

        for relation in self.model.relations[CLUSTER_WORKER_RELATION]:
            self.distributor.allocate_tokens(
                relation=relation,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.WORKER,
            )

    def _create_cos_tokens(self):
        """Create COS tokens and distribute them to peers and workers.

        This method creates COS tokens and distributes them to peers and workers
        if relations exist.
        """
        if not self.model.relations[COS_RELATION]:
            return

        log.info("Prepare cos tokens")
        if rel := self.model.get_relation(COS_TOKENS_RELATION):
            self.distributor.allocate_tokens(
                relation=rel,
                token_strategy=TokenStrategy.COS,
                token_type=ClusterTokenType.CONTROL_PLANE,
            )

        for rel in self.model.relations[COS_TOKENS_WORKER_RELATION]:
            self.distributor.allocate_tokens(
                relation=rel,
                token_strategy=TokenStrategy.COS,
                token_type=ClusterTokenType.WORKER,
            )

    @on_error(
        ops.WaitingStatus("Ensure that the cluster configuration is up-to-date"),
        ReconcilerError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _ensure_cluster_config(self):
        """Ensure that the cluster configuration is up-to-date.

        The snap will detect any changes and only perform necessary steps.
        There is no need to track changes in the charm.
        """
        status.add(ops.MaintenanceStatus("Ensure cluster config"))
        log.info("Ensure cluster-config")

        current_config = self.api_manager.get_cluster_config()

        update_request = UpdateClusterConfigRequest()
        self._configure_datastore(update_request)
        config_changed = update_request.datastore != current_config.metadata.datastore

        update_request.config = assemble_cluster_config(
            self, "external" if self.xcp.has_xcp else None, current_config.metadata.status
        )
        config_changed |= update_request.config != current_config.metadata.status

        configure_kube_control(self)
        if config_changed:
            self.api_manager.update_cluster_config(update_request)

    def _get_scrape_jobs(self):
        """Retrieve the Prometheus Scrape Jobs.

        Returns:
            List[Dict]: A list of metrics endpoints available for scraping.
            Returns an empty list if the token cannot be retrieved or if the
            "cos-tokens" relation does not exist.
        """
        relation = self.model.get_relation(COS_TOKENS_RELATION)
        if not relation:
            log.warning("No cos-tokens available")
            return []

        try:
            with self.collector.recover_token(relation) as token:
                return self.cos.get_metrics_endpoints(
                    self.get_node_name(), token, self.is_control_plane
                )
        except ReconcilerError:
            log.exception("Failed to get COS token.")
        return []

    @on_error(ops.WaitingStatus("Sharing Cluster Version"))
    def _update_kubernetes_version(self):
        """Update the unit Kubernetes version in the cluster relation.

        Raises:
            ReconcilerError: If the cluster integration is missing.
        """
        relation = self.model.get_relation(CLUSTER_RELATION)
        if not relation:
            status.add(ops.BlockedStatus("Missing cluster integration"))
            raise ReconcilerError("Missing cluster integration")
        version, _ = snap_version("k8s")
        if version:
            relation.data[self.unit]["version"] = version

    @on_error(ops.WaitingStatus("Announcing Certificates Provider"))
    def _announce_certificates_config(self) -> None:
        if not (provider := BOOTSTRAP_CERTIFICATES.get(self)):
            raise ReconcilerError("Missing certificates provider")

        for rel in self.model.relations[CLUSTER_WORKER_RELATION]:
            rel.data[self.app][CLUSTER_CERTIFICATES_KEY] = provider
            kubelet_formatter = str(self.config.get(KUBELET_CN_FORMATTER_CONFIG_KEY))
            rel.data[self.app][CLUSTER_CERTIFICATES_KUBELET_FORMATTER_KEY] = kubelet_formatter
            domain_name = str(self.config.get(COMMON_NAME_CONFIG_KEY))
            rel.data[self.app][CLUSTER_CERTIFICATES_DOMAIN_NAME_KEY] = domain_name
        else:
            log.info("Cluster (worker) relation not found, skipping certificates sharing.")
            return

    @on_error(ops.WaitingStatus("Announcing Kubernetes version"))
    def _announce_kubernetes_version(self) -> None:
        """Announce the Kubernetes version to the cluster.

        This method ensures that the Kubernetes version is consistent across the cluster.

        Raises:
            ReconcilerError: If the k8s snap is not installed, the version is missing,
                or the version does not match the local version.
        """
        local_version, _ = snap_version("k8s")
        if not local_version:
            raise ReconcilerError("k8s-snap is not installed")

        relation_config: Dict[str, List[ops.Relation]] = {
            "peer": self.model.relations[CLUSTER_RELATION],
            "worker": self.model.relations[CLUSTER_WORKER_RELATION],
        }

        waiting_units = dict.fromkeys(relation_config, 0)

        for role, relations in relation_config.items():
            for relation in relations:
                if not relation.units:
                    continue

                units = (unit for unit in relation.units if unit.name != self.unit.name)
                for unit in units:
                    unit_version = relation.data[unit].get("version")
                    if not unit_version:
                        raise ReconcilerError(f"Waiting for version from {unit.name}")
                    if unit_version != local_version:
                        waiting_units[role] += 1

                relation.data[self.app]["version"] = local_version

        if not any(waiting_units.values()):
            return

        role_names = {
            "peer": "Control Plane",
            "worker": "Worker",
        }

        waiting_parts = [
            f"{count} {role_names[role]}{'s' if count > 1 else ''}"
            for role, count in waiting_units.items()
            if count
        ]

        status_msg = f"Waiting for {', '.join(waiting_parts)} to upgrade"
        status.add(ops.WaitingStatus(status_msg))
        raise ReconcilerError(status_msg)

    def _get_proxy_systemd_config(self) -> str:
        """Retrieve the Juju model config proxy values.

        Returns:
            str: A multi-line string containing the systemd [Service] section configuration
            with proxy environment variables.
        """
        proxy_env_keys = {
            "JUJU_CHARM_HTTP_PROXY",
            "JUJU_CHARM_HTTPS_PROXY",
            "JUJU_CHARM_NO_PROXY",
        }
        proxy_settings = []
        for key in proxy_env_keys:
            env_value = os.getenv(key)
            if env_value:
                env_key = key.split("JUJU_CHARM_")[-1]
                proxy_settings.append(f"Environment={env_key}={env_value}")
                proxy_settings.append(f"Environment={env_key.lower()}={env_value}")
        if proxy_settings:
            proxy_settings = [
                "[Service]",
                f"# Autogenerated by juju app={self.app.name}",
            ] + proxy_settings
        return "\n".join(proxy_settings)

    @on_error(
        ops.WaitingStatus("Waiting for Cluster token"),
        ReconcilerError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _join_cluster(self, event: ops.EventBase):
        """Retrieve the join token from secret databag and join the cluster.

        Args:
            event (ops.EventBase): event triggering the join
        """
        if not (relation := self.model.get_relation(CLUSTER_RELATION)):
            status.add(ops.BlockedStatus("Missing cluster integration"))
            raise ReconcilerError("Missing cluster integration")

        if local_cluster := self.get_cluster_name():
            self.cloud_integration.integrate(local_cluster, event)
            return

        status.add(ops.MaintenanceStatus("Joining cluster"))
        with self.collector.recover_token(relation) as token:
            remote_cluster = self.collector.cluster_name(relation, False) if relation else ""
            self.cloud_integration.integrate(remote_cluster, event)
            self._join_with_token(token, remote_cluster)

    def _join_with_token(self, token: str, cluster_name: str):
        """Join the cluster with the given token.

        Args:
            token (str): The token to use for joining the cluster.
            cluster_name (str): The name of the cluster to join.
        """
        node_ips = self._get_node_ips()
        node_name = self.get_node_name()
        cluster_addr = f"{node_ips[0]}:{K8SD_PORT}"
        log.info("Joining %s(%s) to %s...", self.unit, node_name, cluster_name)
        request = JoinClusterRequest(name=node_name, address=cluster_addr, token=SecretStr(token))
        if self.is_control_plane:
            request.config = ControlPlaneNodeJoinConfig()
            request.config.extra_sans = self._get_extra_sans()
            config.extra_args.craft(self.config, request.config, cluster_name, node_ips)
        else:
            request.config = NodeJoinConfig()
            config.extra_args.craft(self.config, request.config, cluster_name, node_ips)

            bootstrap_node_taints = BOOTSTRAP_NODE_TAINTS.get(self).strip().split()
            config.extra_args.taint_worker(request.config, bootstrap_node_taints)

        self.certificates.configure_certificates(request.config)
        self.api_manager.join_cluster(request)
        log.info("Joined %s(%s)", self.unit, node_name)

    @on_error(ops.WaitingStatus("Awaiting cluster removal"))
    def _death_handler(self, event: ops.EventBase):
        """Reconcile end of unit's position in the cluster.

        Args:
            event: ops.EventBase - events triggered after notification of removal

        Raises:
            NodeRemovedError: at the end of every loop to prevent the unit from ever reconciling
        """
        if self.lead_control_plane:
            self._revoke_cluster_tokens(event)
        self.update_status.run()
        if self._last_gasp():
            snap_management(self, remove=True)

        relation = self.model.get_relation(CLUSTER_RELATION)
        local_cluster = self.get_cluster_name()
        remote_cluster = self.collector.cluster_name(relation, False) if relation else ""
        if local_cluster and local_cluster != remote_cluster:
            status.add(ops.BlockedStatus("Cannot rejoin new cluster - remove unit"))

        raise NodeRemovedError()

    def _reconcile(self, event: ops.EventBase):
        """Reconcile state change events.

        Args:
            event: ops.EventBase - event that triggered the reconciliation
        """
        log.info("Reconcile event=%s", event)
        if self._evaluate_removal(event):
            self._death_handler(event)

        self.upgrade.handler(event)
        self._apply_proxy_environment()
        self._install_snaps()
        self._apply_snap_requirements()
        self._check_k8sd_ready()
        config.bootstrap.detect_bootstrap_config_changes(self)
        self._update_kubernetes_version()
        if self.lead_control_plane:
            self._k8s_info(event)
            self._configure_external_load_balancer()
            self._check_etcd_ready()
            self._bootstrap_k8s_snap()
            self._ensure_cluster_config()
            self._create_cluster_tokens()
            self._create_cos_tokens()
            self._apply_cos_requirements()
            self._revoke_cluster_tokens(event)
            self._announce_kubernetes_version()
            self._announce_certificates_config()
        self._join_cluster(event)
        self._config_containerd_registries()
        self._configure_cos_integration()
        self.update_status.run()
        self._apply_node_labels()
        self._apply_extra_args()
        if self.is_control_plane:
            self._copy_internal_kubeconfig()
            self._expose_ports()
            self._ensure_cert_sans()

    def _evaluate_removal(self, event: ops.EventBase) -> bool:
        """Determine if my unit is being removed.

        Args:
            event: ops.EventBase - event that triggered charm hook

        Returns:
            True if being removed, otherwise False
        """
        if self._stored.is_dying is True:
            pass
        elif (unit := _cluster_departing_unit(event)) and unit == self.unit:
            # Juju says I am being removed
            self._stored.is_dying = True
        elif (
            isinstance(event, ops.RelationBrokenEvent)
            and event.relation.name == "cluster"
            and self.is_worker
        ):
            # Control-plane never experience RelationBroken on "cluster", it's a peer relation
            # Worker units experience RelationBroken on "cluster" when the relation is removed
            # or this unit is being removed.
            self._stored.is_dying = True
        elif (
            self.is_worker
            and self.get_cluster_name()
            and (relation := self.model.get_relation(CLUSTER_RELATION))
            and not relation.units
        ):
            # If a worker unit has been clustered,
            # but there are no more control-plane units on the relation
            # this unit cannot be re-clustered
            self._stored.is_dying = True
        elif isinstance(event, (ops.RemoveEvent, ops.StopEvent)):
            # If I myself am dying, its me!
            self._stored.is_dying = True
        return bool(self._stored.is_dying)

    def _last_gasp(self):
        """Busy wait on stop event until the unit isn't clustered anymore.

        Returns:
            bool: True if successfully unclustered within time limit, False
                otherwise.
        """
        busy_wait, reported_down = 30, 0
        status.add(ops.MaintenanceStatus("Ensuring cluster removal"))
        while busy_wait and reported_down != 3:
            log.info("Waiting for this unit to uncluster")
            readiness = k8s.node.ready(self.kubeconfig, self.get_node_name())
            if readiness == k8s.node.Status.READY or self.api_manager.is_cluster_bootstrapped():
                log.info("Node is still reportedly clustered")
                reported_down = 0
            else:
                reported_down += 1
            sleep(1)
            busy_wait -= 1
        return reported_down == 3

    @status.on_error(ops.BlockedStatus("Cannot apply node-labels"), LabelMaker.NodeLabelError)
    def _apply_node_labels(self):
        """Apply labels to the node."""
        status.add(ops.MaintenanceStatus("Ensuring Kubernetes Node Labels"))
        node = self.get_node_name()
        if self.labeller.active_labels() is not None:
            self.labeller.apply_node_labels()
            log.info("Node %s labelled successfully", node)
        else:
            log.info("Node %s not yet labelled", node)

    def _apply_extra_args(self):
        """Apply extra args to the node."""
        if cluster_name := self.get_cluster_name():
            status.add(ops.MaintenanceStatus("Ensuring Kubernetes Extra Args"))
            file_args_config = config.arg_files.FileArgsConfig()
            node_ips = self._get_node_ips()
            config.extra_args.craft(self.config, file_args_config, cluster_name, node_ips)
            file_args_config.ensure()

    @property
    def kubeconfig(self) -> Path:
        """Return the highest authority kube config for this unit."""
        return ETC_KUBERNETES / ("admin.conf" if self.is_control_plane else "kubelet.conf")

    @on_error(ops.WaitingStatus(""))
    def _copy_internal_kubeconfig(self):
        """Write internal kubeconfig to /root/.kube/config."""
        status.add(ops.MaintenanceStatus("Regenerating KubeConfig"))
        KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
        KUBECONFIG.write_bytes(self.kubeconfig.read_bytes())

    def _expose_ports(self):
        """Expose ports for public clouds to access api endpoints."""
        log.info("Exposing api ports")
        content = yaml.safe_load(KUBECONFIG.read_text())
        endpoint = urlparse(content["clusters"][0]["cluster"]["server"])
        self.unit.open_port("tcp", endpoint.port)

    def _get_external_kubeconfig(self, event: ops.ActionEvent):
        """Retrieve a public kubeconfig via a charm action.

        Args:
            event: ops.ActionEvent - event that triggered the action
        """
        try:
            server = event.params.get("server")
            if not server:
                log.info("No server requested, use public address")

                try:
                    server = self._get_public_address()
                except LookupError as e:
                    event.fail(f"Failed to get public address: {e}")
                    return

                if not server:
                    event.fail("Failed to get public address. Check logs for details.")
                    return

                port = (
                    str(EXTERNAL_LOAD_BALANCER_PORT)
                    if self.external_load_balancer.is_available
                    else str(APISERVER_PORT)
                )

                server = build_url(server, port, "https")
                log.info("Formatted server address: %s", server)
            log.info("Requesting kubeconfig for server=%s", server)
            resp = self.api_manager.get_kubeconfig(server)
            event.set_results({"kubeconfig": resp})
        except (InvalidResponseError, K8sdConnectionError) as e:
            event.fail(f"Failed to retrieve kubeconfig: {e}")

    def _get_public_address(self) -> str:
        """Get the most public address either from external load balancer or from juju.

        If the external load balancer is available and the unit is a control-plane unit,
        the external load balancer address will be used. Otherwise, the juju public address
        will be used.
        NOTE: Don't ignore the unit's IP in the extra SANs just because there's a load balancer.

        Returns:
            str: The public ip address of the unit.

        Raises:
            LookupError: If it fails to get the external load balancer address.
        """
        try:
            if lb_addr := self.external_load_balancer_address:
                log.info("Using external load balancer address as the public address")
                return lb_addr
        except LookupError as e:
            raise LookupError(f"Failed to get external load balancer address: {e}") from e

        log.info("Using juju public address as the public address")
        return _get_juju_public_address()

    @on_error(
        ops.WaitingStatus("Ensuring SANs are up-to-date"),
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _ensure_cert_sans(self):
        """Ensure the certificate SANs are up-to-date.

        This method checks if the certificate SANs match the required extra SANs.
        If they are not, the certificates are refreshed with the new SANs.
        """
        if not self.is_control_plane:
            return
        if BOOTSTRAP_CERTIFICATES.get(self) == "external":
            # TODO: This should be implemented once k8s-snap offers an API endpoint
            # to update the certificates in the node.
            log.info("External certificates are used, skipping SANs update")
            return

        extra_sans = self._get_extra_sans()
        if not extra_sans:
            log.info("No extra SANs to update")
            return

        dns_sans, ip_sans = get_certificate_sans(APISERVER_CERT)
        all_cert_sans = dns_sans + ip_sans

        missing_sans = [san for san in extra_sans if san not in all_cert_sans]
        if missing_sans:
            all_sans = sorted(set(all_cert_sans) | set(extra_sans))
            log.info(
                "%s not in cert SANs. Refreshing certs with new SANs: %s", missing_sans, all_sans
            )
            status.add(ops.MaintenanceStatus("Refreshing Certificates"))
            if BOOTSTRAP_CERTIFICATES.get(self) == "self-signed":
                self.api_manager.refresh_certs(all_sans)
            elif BOOTSTRAP_CERTIFICATES.get(self) == "external":
                self.certificate_refresh.emit()
            log.info("Certificates have been refreshed")

        log.info("Certificate SANs are up-to-date")

    def _on_refresh_certs_action(self, event: ops.ActionEvent):
        """Handle the refresh-certs action."""
        if self.is_control_plane:
            expires_in = event.params["expires-in"]
            ttl_seconds = utils.ttl_to_seconds(expires_in)
            sans = self._get_extra_sans()
            try:
                self.api_manager.refresh_certs(extra_sans=sans, expiration_seconds=ttl_seconds)
            except (InvalidResponseError, K8sdConnectionError) as e:
                event.fail(f"Failed to refresh certificates: {e}")

    def _check_etcd_ready(self):
        """Check if etcd is ready and update the status accordingly.

        This method initializes the etcd instance and checks its readiness.
        If etcd is not ready, it blocks the charm with an appropriate status.
        """
        if not BOOTSTRAP_DATASTORE.get(self) == DATASTORE_TYPE_EXTERNAL:
            log.info("Not using external etcd, skipping external etcd readiness check")
            return

        legacy_etcd = self.model.get_relation(ETCD_RELATION)
        charmed_etcd = self.model.get_relation(CHARMED_ETCD_RELATION)
        etcd_certificate_relation = self.model.get_relation(ETCD_CERTIFICATES_RELATION)

        if not legacy_etcd and not charmed_etcd:
            msg = "Missing etcd relation"
            log.error(msg)
            status.add(ops.BlockedStatus(msg))
            raise ReconcilerError(msg)

        if legacy_etcd and charmed_etcd:
            msg = "etcd and etcd-client are mutually exclusive. Only one can be active at a time"
            log.error(msg)
            status.add(ops.BlockedStatus(msg))
            raise ReconcilerError(msg)

        if charmed_etcd and not etcd_certificate_relation:
            msg = "etcd-client relation requires etcd-certificates relation"
            log.error(msg)
            status.add(ops.BlockedStatus(msg))
            raise ReconcilerError(msg)

        if etcd_certificate_relation and legacy_etcd:
            msg = "etcd-certificates relation is incompatible with etcd relation"
            log.error(msg)
            status.add(ops.BlockedStatus(msg))
            raise ReconcilerError(msg)

        if (
            etcd_certificate_relation
            and not self.etcd_certificate.certificates.get_assigned_certificates()[0]
        ):
            msg = "Waiting for the etcd client certificate"
            log.error(msg)
            status.add(ops.WaitingStatus(msg))
            raise ReconcilerError(msg)

    def _initialize_external_etcd(self) -> Union[EtcdReactiveRequires, CharmedEtcdRequires, None]:
        """Initialize etcd instance or block charm."""
        legacy_etcd = self.model.get_relation(ETCD_RELATION)
        charmed_etcd = self.model.get_relation(CHARMED_ETCD_RELATION)

        if legacy_etcd:
            log.info("Using legacy etcd relation")
            return EtcdReactiveRequires(self)

        if charmed_etcd:
            log.info("Using charmed etcd relation")
            return CharmedEtcdRequires(self, self.etcd_certificate.certificates)


if __name__ == "__main__":  # pragma: nocover
    ops.main(K8sCharm)
