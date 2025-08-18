# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import dataclasses
import logging
from typing import List, Optional

import ops
import pki
from literals import (
    BOOTSTRAP_CERTIFICATES,
    BOOTSTRAP_DATASTORE,
    BOOTSTRAP_NODE_TAINTS,
    BOOTSTRAP_POD_CIDR,
    BOOTSTRAP_SERVICE_CIDR,
    CLUSTER_CERTIFICATES_KEY,
    CLUSTER_RELATION,
    CLUSTER_WORKER_RELATION,
    DATASTORE_NAME_MAPPING,
    DEFAULT_CERTIFICATE_PROVIDER,
    SUPPORTED_CERTIFICATES,
)
from ops.interface_kube_control.model import Taint
from protocols import K8sCharmProtocol
from pydantic import TypeAdapter, ValidationError

import charms.contextual_status as context_status
import charms.k8s.v0.k8sd_api_manager as k8sd

log = logging.getLogger(__name__)


@dataclasses.dataclass
class ConfigOptions:
    """Charm options that has a `bootstrap-` prefix."""

    datastore: Optional[str] = dataclasses.field(
        default=None, metadata={"alias": BOOTSTRAP_DATASTORE.name}
    )
    pod_cidr: Optional[str] = dataclasses.field(
        default=None, metadata={"alias": BOOTSTRAP_POD_CIDR.name}
    )
    service_cidr: Optional[str] = dataclasses.field(
        default=None, metadata={"alias": BOOTSTRAP_SERVICE_CIDR.name}
    )
    certificates: str = dataclasses.field(
        default="", metadata={"alias": BOOTSTRAP_CERTIFICATES.name}
    )


def _load_certificates_provider(charm: K8sCharmProtocol) -> str:
    """Load the certificate provider name from the cluster relation.

    Args:
        charm: An instance of the charm.

    Returns:
        str: The certificate provider name.
    """
    app = charm.is_control_plane and charm.app or None
    relation = charm.model.get_relation(CLUSTER_RELATION)
    provider = relation and relation.data[app or relation.app].get(CLUSTER_CERTIFICATES_KEY)

    if not provider:
        # Note(AKD): This could be an upgrade scenario where the provider is unset.
        # if this node is online, we know that a certificates provider is already set.
        try:
            charm.api_manager.get_node_status().metadata
            provider = DEFAULT_CERTIFICATE_PROVIDER if pki.check_ca_key() else "external"
        except (k8sd.K8sdConnectionError, k8sd.InvalidResponseError) as e:
            log.error("Failed to get node status: %s", e)

    return provider or ""


def _persist_certificates_provider(charm: K8sCharmProtocol, provider: str) -> None:
    """Persist the certificates provider in the cluster relation.

    Write-once into the cluster relation at the cluster-certificates key.

    Args:
        charm: An instance of the charm.
        provider (str): The certificates provider to persist.
    """
    peers = charm.model.relations[CLUSTER_RELATION]
    workers = charm.model.relations.get(CLUSTER_WORKER_RELATION, [])
    for relation in peers + workers:
        app_data = relation.data[charm.app]
        if not app_data.get(CLUSTER_CERTIFICATES_KEY):
            app_data[CLUSTER_CERTIFICATES_KEY] = provider


class Controller:
    """A store for bootstrap configuration options."""

    def __init__(self, charm: K8sCharmProtocol) -> None:
        """Initialize the BootstrapStore instance.

        Args:
            charm: The charm instance.
        """
        self._charm = charm
        self.immutable = self.load_immutable()

    def load_immutable(self) -> ConfigOptions:
        """Load the bootstrap immutable storage options.

        Args:
            charm: The charm instance.

        Returns:
            A BootstrapStore instance with the configuration options.
        """
        opts = ConfigOptions()
        opts.certificates = _load_certificates_provider(self._charm)

        # Load from the immutable cluster storage.
        try:
            if self._charm.is_control_plane:
                cluster = self._charm.api_manager.get_cluster_config().metadata
                snap_ds = cluster.datastore and cluster.datastore.type
                opts.datastore = {v: k for k, v in DATASTORE_NAME_MAPPING.items()}.get(snap_ds)
                opts.pod_cidr = cluster.pod_cidr
                opts.service_cidr = cluster.service_cidr
        except (k8sd.K8sdConnectionError, k8sd.InvalidResponseError) as e:
            log.warning("Cannot load cluster config: %s", e)

        return opts

    @property
    def config(self) -> ConfigOptions:
        """Return the current bootstrap configuration options."""
        immutable, with_auto = self.immutable, self._with_auto
        return ConfigOptions(
            datastore=immutable.datastore or with_auto.datastore,
            pod_cidr=immutable.pod_cidr or with_auto.pod_cidr,
            service_cidr=immutable.service_cidr or with_auto.service_cidr,
            certificates=immutable.certificates or with_auto.certificates,
        )

    def validate(self) -> None:
        """Validate the bootstrap options."""
        config = self.config
        try:
            if config.datastore not in DATASTORE_NAME_MAPPING:
                name = BOOTSTRAP_DATASTORE.name
                drop_none = DATASTORE_NAME_MAPPING.keys() - {None}
                log.error(
                    "Invalid %s: %s. Valid Options are: %s",
                    name,
                    config.datastore,
                    ", ".join(sorted(drop_none)),
                )
                raise ValueError(f"{name}='{config.datastore}' is invalid.")
            if config.certificates not in SUPPORTED_CERTIFICATES:
                name = BOOTSTRAP_CERTIFICATES.name
                log.error(
                    "Invalid %s: %s. Valid Options are: %s",
                    name,
                    config.certificates,
                    ", ".join(sorted(SUPPORTED_CERTIFICATES)),
                )
                raise ValueError(f"{name}='{config.certificates}' is invalid.")
        except ValueError as e:
            m = str(e)
            log.error("Invalid bootstrap configuration: %s", m)
            context_status.add(ops.BlockedStatus(m))
            raise context_status.ReconcilerError(m) from e

    def persist(self) -> None:
        """Persist the bootstrap configuration options."""
        config = self.config
        self.immutable.datastore = config.datastore
        self.immutable.pod_cidr = config.pod_cidr
        self.immutable.service_cidr = config.service_cidr
        self.immutable.certificates = self.persist_certificates()

    def persist_certificates(self) -> str:
        """Persist the certificates provider in the cluster relation."""
        if not (provider := self.config.certificates):
            raise context_status.ReconcilerError("Missing certificates provider")

        _persist_certificates_provider(self._charm, provider)
        return provider

    @property
    def _juju(self) -> ConfigOptions:
        """Return the bootstrap configuration options from the juju config.

        Options are always loaded from the charm config, or mapped through the default
        if they are set to "auto".
        """
        opts = ConfigOptions()
        if self._charm.is_control_plane:
            opts.certificates = BOOTSTRAP_CERTIFICATES.get(self._charm)
            opts.datastore = BOOTSTRAP_DATASTORE.get(self._charm)
            opts.pod_cidr = BOOTSTRAP_POD_CIDR.get(self._charm)
            opts.service_cidr = BOOTSTRAP_SERVICE_CIDR.get(self._charm)

        return opts

    @property
    def _with_auto(self) -> ConfigOptions:
        """Return the bootstrap configuration options from the juju config with auto-mapping.

        Options are always loaded from the charm config, or mapped through the default
        if they are set to "auto".
        """
        opts = ConfigOptions()
        juju = self._juju
        if self._charm.is_control_plane:
            # Default to self-signed only if the charm is a control plane.
            opts.certificates = DEFAULT_CERTIFICATE_PROVIDER
            if (val := juju.datastore) != "auto":
                opts.datastore = val
            if (val := juju.certificates) != "auto":
                opts.certificates = val
            if (val := juju.pod_cidr) != "auto":
                opts.pod_cidr = val
            if (val := juju.service_cidr) != "auto":
                opts.service_cidr = val

        return opts


@context_status.on_error(
    ops.BlockedStatus("Invalid config on bootstrap-node-taints"),
    ValidationError,
)
def node_taints(charm: ops.CharmBase) -> List[str]:
    """Share node taints with the kube-control interface."""
    taints = BOOTSTRAP_NODE_TAINTS.get(charm).split()
    for taint in taints:
        TypeAdapter(Taint).validate_python(taint)
    return taints
