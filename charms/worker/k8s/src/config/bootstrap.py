# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import dataclasses
import logging
from typing import List, Optional

import ops
from literals import (
    BOOTSTRAP_DATASTORE,
    BOOTSTRAP_NODE_TAINTS,
    BOOTSTRAP_POD_CIDR,
    BOOTSTRAP_SERVICE_CIDR,
    DATASTORE_NAME_MAPPING,
)
from ops.interface_kube_control.model import Taint
from protocols import K8sCharmProtocol

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

    @property
    def _juju(self) -> ConfigOptions:
        """Return the bootstrap configuration options from the juju config.

        Options are always loaded from the charm config, or mapped through the default
        if they are set to "auto".
        """
        opts = ConfigOptions()
        if self._charm.is_control_plane:
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
            if (val := juju.datastore) != "auto":
                opts.datastore = val
            if (val := juju.pod_cidr) != "auto":
                opts.pod_cidr = val
            if (val := juju.service_cidr) != "auto":
                opts.service_cidr = val

        return opts


@context_status.on_error(
    ops.BlockedStatus("Invalid config on bootstrap-node-taints"), TypeError, ValueError
)
def node_taints(charm: ops.CharmBase) -> List[str]:
    """Share node taints with the kube-control interface."""
    taints = BOOTSTRAP_NODE_TAINTS.get(charm).split()
    for taint in taints:
        Taint.validate(taint)
    return taints
