#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
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
import logging
import os
import shlex
import socket
import subprocess
from functools import cached_property
from pathlib import Path
from time import sleep
from typing import Dict, Optional, Union
from urllib.parse import urlparse

import charms.contextual_status as status
import charms.operator_libs_linux.v2.snap as snap_lib
import containerd
import ops
import reschedule
import yaml
from charms.contextual_status import ReconcilerError, WaitingStatus, on_error
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    CreateClusterRequest,
    DNSConfig,
    GatewayConfig,
    InvalidResponseError,
    JoinClusterRequest,
    K8sdAPIManager,
    K8sdConnectionError,
    LocalStorageConfig,
    NetworkConfig,
    UnixSocketConnectionFactory,
    UpdateClusterConfigRequest,
    UserFacingClusterConfig,
    UserFacingDatastoreConfig,
)
from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from charms.node_base import LabelMaker
from charms.reconciler import Reconciler
from cloud_integration import CloudIntegration
from cos_integration import COSIntegration
from inspector import ClusterInspector
from kube_control import configure as configure_kube_control
from literals import DEPENDENCIES
from ops.interface_kube_control import KubeControlProvides
from snap import management as snap_management
from snap import version as snap_version
from token_distributor import ClusterTokenType, TokenCollector, TokenDistributor, TokenStrategy
from typing_extensions import Literal
from upgrade import K8sDependenciesModel, K8sUpgrade

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]
K8SD_SNAP_SOCKET = "/var/snap/k8s/common/var/lib/k8sd/state/control.socket"
KUBECONFIG = Path.home() / ".kube/config"
ETC_KUBERNETES = Path("/etc/kubernetes")
KUBECTL_PATH = Path("/snap/k8s/current/bin/kubectl")
K8SD_PORT = 6400
SUPPORTED_DATASTORES = ["dqlite", "etcd"]


def _get_public_address() -> str:
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
    """

    _stored = ops.StoredState()

    def __init__(self, *args):
        """Initialise the K8s charm.

        Args:
            args: Arguments passed to the CharmBase parent constructor.
        """
        super().__init__(*args)
        factory = UnixSocketConnectionFactory(unix_socket=K8SD_SNAP_SOCKET, timeout=320)
        self.api_manager = K8sdAPIManager(factory)
        xcp_relation = "external-cloud-provider" if self.is_control_plane else ""
        self.cloud_integration = CloudIntegration(self, self.is_control_plane)
        self.xcp = ExternalCloudProvider(self, xcp_relation)
        self.cluster_inspector = ClusterInspector(kubeconfig_path=KUBECONFIG)
        self.upgrade = K8sUpgrade(
            self,
            node_manager=self.cluster_inspector,
            relation_name="upgrade",
            substrate="vm",
            dependency_model=K8sDependenciesModel(**DEPENDENCIES),
        )
        self.cos = COSIntegration(self)
        self.reconciler = Reconciler(self, self._reconcile)
        self.distributor = TokenDistributor(self, self.get_node_name(), self.api_manager)
        self.collector = TokenCollector(self, self.get_node_name())
        self.labeller = LabelMaker(
            self, kubeconfig_path=self._internal_kubeconfig, kubectl=KUBECTL_PATH
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
            ],
        )

        self.framework.observe(self.on.update_status, self._on_update_status)
        if self.is_control_plane:
            self.etcd = EtcdReactiveRequires(self)
            self.kube_control = KubeControlProvides(self, endpoint="kube-control")
            self.framework.observe(self.on.get_kubeconfig_action, self._get_external_kubeconfig)

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
        AssertionError,
    )
    def _apply_cos_requirements(self):
        """Apply COS requirements for integration.

        This method applies COS requirements for integration. It configures COS
        Integration by applying the manifests for COS Cluster Roles and
        kube-state-metrics (K-S-M).
        """
        if not self.model.get_relation("cos-agent"):
            return

        log.info("Apply COS Integrations")
        status.add(ops.MaintenanceStatus("Ensuring COS Integration"))
        subprocess.check_call(shlex.split("k8s kubectl apply -f templates/cos_roles.yaml"))
        subprocess.check_call(shlex.split("k8s kubectl apply -f templates/ksm.yaml"))

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

    def _apply_proxy_environment(self):
        """Apply the proxy settings from environment variables."""
        proxy_settings = self._get_proxy_env()
        if proxy_settings:
            log.info("Applying Proxied Environment Settings")
            with open("/etc/environment", mode="r", encoding="utf-8") as file:
                current_env = dict(line.strip().split("=", 1) for line in file if "=" in line)

            current_env.update(proxy_settings)
            with open("/etc/environment", mode="w", encoding="utf-8") as file:
                file.write("\n".join([f"{k}={v}" for k, v in current_env.items()]))

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
        if self._stored.cluster_name == "":
            if self.lead_control_plane and self.api_manager.is_cluster_bootstrapped():
                self._stored.cluster_name = self._generate_unique_cluster_name()
            elif not (relation := self.model.get_relation("cluster")):
                pass
            elif any(
                [
                    self.is_control_plane and self.api_manager.is_cluster_bootstrapped(),
                    self._is_node_present(),
                ]
            ):
                self._stored.cluster_name = self.collector.cluster_name(relation, True)

        unit, node = self.unit.name, self.get_node_name()
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
        snap_management()

    @on_error(WaitingStatus("Waiting to apply snap requirements"), subprocess.CalledProcessError)
    def _apply_snap_requirements(self):
        """Apply necessary snap requirements for the k8s snap.

        This method executes necessary scripts to ensure that the snap
        meets the network and interface requirements.
        """
        status.add(ops.MaintenanceStatus("Ensuring snap requirements"))
        log.info("Applying K8s requirements")
        init_sh = "/snap/k8s/current/k8s/hack/init.sh"
        subprocess.check_call(shlex.split(init_sh))

    @on_error(WaitingStatus("Waiting for k8sd"), InvalidResponseError, K8sdConnectionError)
    def _check_k8sd_ready(self):
        """Check if k8sd is ready to accept requests."""
        log.info("Check if k8ds is ready")
        status.add(ops.MaintenanceStatus("Ensuring snap readiness"))
        self.api_manager.check_k8sd_ready()

    @on_error(
        ops.WaitingStatus("Waiting to bootstrap k8s snap"),
        AssertionError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _bootstrap_k8s_snap(self):
        """Bootstrap k8s if it's not already bootstrapped."""
        if self.api_manager.is_cluster_bootstrapped():
            log.info("K8s cluster already bootstrapped")
            return

        bootstrap_config = BootstrapConfig.construct()
        self._configure_datastore(bootstrap_config)
        bootstrap_config.cluster_config = self._assemble_cluster_config()
        bootstrap_config.service_cidr = str(self.config["service-cidr"])
        bootstrap_config.control_plane_taints = str(self.config["register-with-taints"]).split()
        bootstrap_config.extra_sans = [_get_public_address()]
        bootstrap_config.extra_node_kube_controller_manager_args = {
            "--cluster-name": self._generate_unique_cluster_name()
        }

        status.add(ops.MaintenanceStatus("Bootstrapping Cluster"))

        binding = self.model.get_binding("cluster")
        address = binding and binding.network.ingress_address
        node_name = self.get_node_name()
        payload = CreateClusterRequest(
            name=node_name, address=f"{address}:{K8SD_PORT}", config=bootstrap_config
        )

        # TODO: Make port (and address) configurable.
        self.api_manager.bootstrap_k8s_snap(payload)

    @on_error(
        ops.BlockedStatus("Failed to apply containerd_custom_registries, check logs for details"),
        ValueError,
        subprocess.CalledProcessError,
        OSError,
    )
    def _config_containerd_registries(self):
        """Apply containerd custom registries."""
        registries, config = [], ""
        containerd_relation = self.model.get_relation("containerd")
        if self.is_control_plane:
            config = str(self.config["containerd_custom_registries"])
            registries = containerd.parse_registries(config)
        else:
            registries = containerd.recover(containerd_relation)
        self.unit.status = ops.MaintenanceStatus("Ensuring containerd registries")
        containerd.ensure_registry_configs(registries)
        if self.lead_control_plane:
            containerd.share(config, self.app, containerd_relation)

    def _configure_cos_integration(self):
        """Retrieve the join token from secret databag and join the cluster."""
        if not self.model.get_relation("cos-agent"):
            return

        status.add(ops.MaintenanceStatus("Updating COS integrations"))
        log.info("Updating COS integration")
        if relation := self.model.get_relation("cos-tokens"):
            self.collector.request(relation)

    def _get_valid_annotations(self) -> Optional[dict]:
        """Fetch and validate annotations from charm configuration.

        The values are expected to be a space-separated string of key-value pairs.

        Returns:
            dict: The parsed annotations if valid, otherwise None.

        Raises:
            AssertionError: If any annotation is invalid.
        """
        raw_annotations = self.config.get("annotations")
        if not raw_annotations:
            return None

        raw_annotations = str(raw_annotations)

        annotations = {}
        try:
            for key, value in [pair.split("=", 1) for pair in raw_annotations.split()]:
                assert key and value, "Invalid Annotation"  # nosec
                annotations[key] = value
        except AssertionError:
            log.exception("Invalid annotations: %s", raw_annotations)
            status.add(ops.BlockedStatus("Invalid Annotations"))
            raise

        return annotations

    def _assemble_cluster_config(self) -> UserFacingClusterConfig:
        """Retrieve the cluster config from charm configuration and charm relations.

        Returns:
            UserFacingClusterConfig: The expected cluster configuration.
        """
        local_storage = LocalStorageConfig(
            enabled=self.config.get("local-storage-enabled"),
            local_path=self.config.get("local-storage-local-path"),
            reclaim_policy=self.config.get("local-storage-reclaim-policy"),
            # Note(ben): set_default is intentionally omitted, see:
            # https://github.com/canonical/k8s-operator/pull/169/files#r1847378214
        )

        gateway = GatewayConfig(
            enabled=self.config.get("gateway-enabled"),
        )

        cloud_provider = None
        if self.xcp.has_xcp:
            cloud_provider = "external"

        return UserFacingClusterConfig(
            local_storage=local_storage,
            gateway=gateway,
            annotations=self._get_valid_annotations(),
            cloud_provider=cloud_provider,
        )

    def _configure_datastore(self, config: Union[BootstrapConfig, UpdateClusterConfigRequest]):
        """Configure the datastore for the Kubernetes cluster.

        Args:
            config (BootstrapConfig|UpdateClusterConfigRequst):
                The configuration object for the Kubernetes cluster. This object
                will be modified in-place to include etcd's configuration details.
        """
        datastore = self.config.get("datastore")

        if datastore not in SUPPORTED_DATASTORES:
            log.error(
                "Invalid datastore: %s. Supported values: %s",
                datastore,
                ", ".join(SUPPORTED_DATASTORES),
            )
            status.add(ops.BlockedStatus(f"Invalid datastore: {datastore}"))
        assert datastore in SUPPORTED_DATASTORES  # nosec

        if datastore == "etcd":
            log.info("Using etcd as external datastore")
            etcd_relation = self.model.get_relation("etcd")

            assert etcd_relation, "Missing etcd relation"  # nosec
            assert self.etcd.is_ready, "etcd is not ready"  # nosec

            etcd_config = self.etcd.get_client_credentials()
            if isinstance(config, BootstrapConfig):
                config.datastore_type = "external"
                config.datastore_ca_cert = etcd_config.get("client_ca", "")
                config.datastore_client_cert = etcd_config.get("client_cert", "")
                config.datastore_client_key = etcd_config.get("client_key", "")
                config.datastore_servers = self.etcd.get_connection_string().split(",")
                log.info("etcd servers: %s", config.datastore_servers)
            elif isinstance(config, UpdateClusterConfigRequest):
                config.datastore = UserFacingDatastoreConfig()
                config.datastore.type = "external"
                config.datastore.servers = self.etcd.get_connection_string().split(",")
                config.datastore.ca_crt = etcd_config.get("client_ca", "")
                config.datastore.client_crt = etcd_config.get("client_cert", "")
                config.datastore.client_key = etcd_config.get("client_key", "")
                log.info("etcd servers: %s", config.datastore.servers)

        elif datastore == "dqlite":
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

        if peer := self.model.get_relation("cluster"):
            self.distributor.revoke_tokens(
                relation=peer,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.CONTROL_PLANE,
                to_remove=to_remove,
            )

        if workers := self.model.get_relation("k8s-cluster"):
            self.distributor.revoke_tokens(
                relation=workers,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.WORKER,
                to_remove=to_remove,
            )

    def _create_cluster_tokens(self):
        """Create tokens for the units in the cluster and k8s-cluster relations."""
        log.info("Prepare clustering")
        if peer := self.model.get_relation("cluster"):
            self.distributor.allocate_tokens(
                relation=peer,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.CONTROL_PLANE,
            )

        if workers := self.model.get_relation("k8s-cluster"):
            self.distributor.allocate_tokens(
                relation=workers,
                token_strategy=TokenStrategy.CLUSTER,
                token_type=ClusterTokenType.WORKER,
            )

    def _create_cos_tokens(self):
        """Create COS tokens and distribute them to peers and workers.

        This method creates COS tokens and distributes them to peers and workers
        if relations exist.
        """
        if not self.model.get_relation("cos-agent"):
            return

        log.info("Prepare cos tokens")
        if rel := self.model.get_relation("cos-tokens"):
            self.distributor.allocate_tokens(
                relation=rel,
                token_strategy=TokenStrategy.COS,
                token_type=ClusterTokenType.CONTROL_PLANE,
            )

        if rel := self.model.get_relation("cos-worker-tokens"):
            self.distributor.allocate_tokens(
                relation=rel,
                token_strategy=TokenStrategy.COS,
                token_type=ClusterTokenType.WORKER,
            )

    @on_error(
        WaitingStatus("Waiting to enable features"),
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _enable_functionalities(self):
        """Enable necessary components for the Kubernetes cluster."""
        status.add(ops.MaintenanceStatus("Updating K8s features"))
        log.info("Enabling K8s features")
        dns_config = DNSConfig(enabled=True)
        network_config = NetworkConfig(enabled=True)
        local_storage_config = LocalStorageConfig(enabled=True)
        user_cluster_config = UserFacingClusterConfig(
            dns=dns_config, network=network_config, local_storage=local_storage_config
        )
        update_request = UpdateClusterConfigRequest(config=user_cluster_config)

        self.api_manager.update_cluster_config(update_request)

    @on_error(
        WaitingStatus("Ensure that the cluster configuration is up-to-date"),
        AssertionError,
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

        update_request = UpdateClusterConfigRequest()

        self._configure_datastore(update_request)
        update_request.config = self._assemble_cluster_config()
        configure_kube_control(self)
        self.api_manager.update_cluster_config(update_request)

    def _get_scrape_jobs(self):
        """Retrieve the Prometheus Scrape Jobs.

        Returns:
            List[Dict]: A list of metrics endpoints available for scraping.
            Returns an empty list if the token cannot be retrieved or if the
            "cos-tokens" relation does not exist.
        """
        relation = self.model.get_relation("cos-tokens")
        if not relation:
            log.warning("No cos-tokens available")
            return []

        try:
            with self.collector.recover_token(relation) as token:
                return self.cos.get_metrics_endpoints(
                    self.get_node_name(), token, self.is_control_plane
                )
        except AssertionError:
            log.exception("Failed to get COS token.")
        return []

    @on_error(ops.WaitingStatus("Sharing Cluster Version"))
    def _update_kubernetes_version(self):
        """Update the unit Kubernetes version in the cluster relation.

        Raises:
            ReconcilerError: If the cluster integration is missing.
        """
        relation = self.model.get_relation("cluster")
        if not relation:
            status.add(ops.BlockedStatus("Missing cluster integration"))
            raise ReconcilerError("Missing cluster integration")
        if version := snap_version("k8s"):
            relation.data[self.unit]["version"] = version

    @on_error(ops.WaitingStatus("Announcing Kubernetes version"))
    def _announce_kubernetes_version(self):
        """Announce the Kubernetes version to the cluster.

        This method ensures that the Kubernetes version is consistent across the cluster.

        Raises:
            ReconcilerError: If the k8s snap is not installed, the version is missing,
                or the version does not match the local version.
        """
        if not (local_version := snap_version("k8s")):
            raise ReconcilerError("k8s-snap is not installed")

        peer = self.model.get_relation("cluster")
        worker = self.model.get_relation("k8s-cluster")

        for relation in (peer, worker):
            if not relation:
                continue
            units = (unit for unit in relation.units if unit.name != self.unit.name)
            for unit in units:
                unit_version = relation.data[unit].get("version")
                if not unit_version:
                    raise ReconcilerError(f"Waiting for version from {unit.name}")
                if unit_version != local_version:
                    status.add(ops.BlockedStatus(f"Version mismatch with {unit.name}"))
                    raise ReconcilerError(f"Version mismatch with {unit.name}")
            relation.data[self.app]["version"] = local_version

    def _get_proxy_env(self) -> Dict[str, str]:
        """Retrieve the Juju model config proxy values.

        Returns:
            Dict: A dictionary containing the proxy settings,
                or None if no values are configured.
        """
        proxy_env_keys = {
            "JUJU_CHARM_HTTP_PROXY",
            "JUJU_CHARM_HTTPS_PROXY",
            "JUJU_CHARM_NO_PROXY",
        }
        proxy_settings = {}
        for key in proxy_env_keys:
            env_key = key.split("JUJU_CHARM_")[-1]
            env_value = os.getenv(key)
            if env_value:
                proxy_settings[env_key] = env_value
                proxy_settings[env_key.lower()] = env_value
        return proxy_settings

    @on_error(
        WaitingStatus("Waiting for Cluster token"),
        AssertionError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _join_cluster(self, event: ops.EventBase):
        """Retrieve the join token from secret databag and join the cluster.

        Args:
            event (ops.EventBase): event triggering the join
        """
        if not (relation := self.model.get_relation("cluster")):
            status.add(ops.BlockedStatus("Missing cluster integration"))
            raise ReconcilerError("Missing cluster integration")

        if local_cluster := self.get_cluster_name():
            self.cloud_integration.integrate(local_cluster, event)
            return

        status.add(ops.MaintenanceStatus("Joining cluster"))
        with self.collector.recover_token(relation) as token:
            remote_cluster = self.collector.cluster_name(relation, False) if relation else ""
            self.cloud_integration.integrate(remote_cluster, event)
            self._join_with_token(relation, token)

    def _join_with_token(self, relation: ops.Relation, token: str):
        """Join the cluster with the given token.

        Args:
            relation (ops.Relation): The relation to use for the token.
            token (str): The token to use for joining the cluster.
        """
        binding = self.model.get_binding(relation.name)
        address = binding and binding.network.ingress_address
        node_name = self.get_node_name()
        cluster_addr = f"{address}:{K8SD_PORT}"
        log.info("Joining %s(%s) to %s...", self.unit, node_name, cluster_addr)
        request = JoinClusterRequest(name=node_name, address=cluster_addr, token=token)
        if self.is_control_plane:
            request.config = ControlPlaneNodeJoinConfig()
            request.config.extra_sans = [_get_public_address()]

        self.api_manager.join_cluster(request)
        log.info("Joined %s(%s)", self.unit, node_name)

    @on_error(WaitingStatus("Awaiting cluster removal"))
    def _death_handler(self, event: ops.EventBase):
        """Reconcile end of unit's position in the cluster.

        Args:
            event: ops.EventBase - events triggered after notification of removal

        Raises:
            NodeRemovedError: at the end of every loop to prevent the unit from ever reconciling
        """
        if self.lead_control_plane:
            self._revoke_cluster_tokens(event)
        self._update_status()
        self._last_gasp()

        relation = self.model.get_relation("cluster")
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

        self._apply_proxy_environment()
        self._install_snaps()
        self._apply_snap_requirements()
        self._check_k8sd_ready()
        self._update_kubernetes_version()
        if self.lead_control_plane:
            self._k8s_info(event)
            self._bootstrap_k8s_snap()
            self._enable_functionalities()
            self._create_cluster_tokens()
            self._create_cos_tokens()
            self._apply_cos_requirements()
            self._revoke_cluster_tokens(event)
            self._ensure_cluster_config()
            self._announce_kubernetes_version()
        self._join_cluster(event)
        self._config_containerd_registries()
        self._configure_cos_integration()
        self._update_status()
        self._apply_node_labels()
        if self.is_control_plane:
            self._copy_internal_kubeconfig()
            self._expose_ports()

    def _update_status(self):
        """Check k8s snap status."""
        if version := snap_version("k8s"):
            self.unit.set_workload_version(version)

        if not self.get_cluster_name():
            status.add(ops.WaitingStatus("Node not Clustered"))
            return

        trigger = reschedule.PeriodicEvent(self)
        if not self._is_node_ready():
            status.add(ops.WaitingStatus("Node not Ready"))
            trigger.create(reschedule.Period(seconds=30))
            return
        trigger.cancel()

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
            and (relation := self.model.get_relation("cluster"))
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

    def _is_node_present(self, node: str = "") -> bool:
        """Determine if node is in the kubernetes cluster.

        Args:
            node (str): name of node

        Returns:
            bool: True when this unit appears in the node list
        """
        node = node or self.get_node_name()
        cmd = ["nodes", node, "-o=jsonpath={.metadata.name}"]
        try:
            return self.kubectl_get(*cmd) == node
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _is_node_ready(self, node: str = "") -> bool:
        """Determine if node is Ready in the kubernetes cluster.

        Args:
            node (str): name of node

        Returns:
            bool: True when this unit is marked as Ready
        """
        node = node or self.get_node_name()
        cmd = ["nodes", node, '-o=jsonpath={.status.conditions[?(@.type=="Ready")].status}']
        try:
            return self.kubectl_get(*cmd) == "True"
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _last_gasp(self):
        """Busy wait on stop event until the unit isn't clustered anymore."""
        busy_wait, reported_down = 30, 0
        status.add(ops.MaintenanceStatus("Ensuring cluster removal"))
        while busy_wait and reported_down != 3:
            log.info("Waiting for this unit to uncluster")
            if self._is_node_ready() or self.api_manager.is_cluster_bootstrapped():
                log.info("Node is still reportedly clustered")
                reported_down = 0
            else:
                reported_down += 1
            sleep(1)
            busy_wait -= 1

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

    def _on_update_status(self, _event: ops.UpdateStatusEvent):
        """Handle update-status event."""
        if not self.reconciler.stored.reconciled:
            return

        try:
            with status.context(self.unit):
                self._update_status()
        except status.ReconcilerError:
            log.exception("Can't update_status")

    def kubectl(self, *args) -> str:
        """Run kubectl command.

        Arguments:
            args: arguments passed to kubectl

        Returns:
            string response

        Raises:
            CalledProcessError: in the event of a failed kubectl
        """
        cmd = [KUBECTL_PATH, f"--kubeconfig={self._internal_kubeconfig}", *args]
        log.info("Executing %s", cmd)
        try:
            return subprocess.check_output(cmd, text=True)
        except subprocess.CalledProcessError as e:
            log.error(
                "Command failed: %s}\nreturncode: %s\nstdout: %s", cmd, e.returncode, e.output
            )
            raise

    def kubectl_get(self, *args) -> str:
        """Run kubectl get command.

        Arguments:
            args: arguments passed to kubectl get

        Returns:
            string response
        """
        return self.kubectl("get", *args)

    @property
    def _internal_kubeconfig(self) -> Path:
        """Return the highest authority kube config for this unit."""
        return ETC_KUBERNETES / ("admin.conf" if self.is_control_plane else "kubelet.conf")

    @on_error(ops.WaitingStatus(""))
    def _copy_internal_kubeconfig(self):
        """Write internal kubeconfig to /root/.kube/config."""
        status.add(ops.MaintenanceStatus("Regenerating KubeConfig"))
        KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
        KUBECONFIG.write_bytes(self._internal_kubeconfig.read_bytes())

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
                log.info("No server requested, use public-address")
                server = f"{_get_public_address()}:6443"
            log.info("Requesting kubeconfig for server=%s", server)
            resp = self.api_manager.get_kubeconfig(server)
            event.set_results({"kubeconfig": resp})
        except (InvalidResponseError, K8sdConnectionError) as e:
            event.fail(f"Failed to retrieve kubeconfig: {e}")


if __name__ == "__main__":  # pragma: nocover
    ops.main(K8sCharm)
