# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import collections
import dataclasses
import ipaddress
import logging
from typing import List, Optional, Set, Union

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
AnyIPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


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


def valid_cidr(cidr: str, name: str, required: bool = False) -> Set[AnyIPNetwork]:
    """Validate a CIDR block.

    Args:
        cidr: The CIDR block to validate.
        name: The name of the configuration option for logging.
        required: Whether the CIDR block is required.

    Raises:
        ValueError: If the CIDR block is invalid.
    """
    if not cidr and required:
        raise ValueError(f"{name} is required.")

    user_cidr: List[AnyIPNetwork] = []
    for cidr in cidr.strip().split(","):
        if not cidr.strip():
            continue
        net = ipaddress.ip_network(cidr)  # Validate the CIDR format.
        if net.prefixlen == net.max_prefixlen:
            raise ValueError(f"CIDR '{cidr}' is a single IP address.")
        user_cidr.append(net)

    if not (1 <= len(user_cidr) <= 2):
        raise ValueError(f"{name} must contain 1 or 2 CIDR blocks, not {len(user_cidr)}.")

    if len(user_cidr) == 2:
        counter = collections.Counter(ipaddress.ip_network(c).version for c in user_cidr)
        if counter[4] != 1 or counter[6] != 1:
            raise ValueError(f"{name} must contain one IPv4 and one IPv6 CIDR block.")
    return set(user_cidr)


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
        if self._charm.is_worker:
            log.debug("Loaded immutable config for worker")
            return opts

        if not self._charm.api_manager.is_cluster_bootstrapped():
            log.debug("Cluster not bootstrapped, no immutable cluster config")
            return opts

        try:
            cluster = self._charm.api_manager.get_cluster_config().metadata
            opts.pod_cidr = cluster.pod_cidr
            opts.service_cidr = cluster.service_cidr
            snap_ds = cluster.datastore and cluster.datastore.type
            if not snap_ds:
                # Fallback to the status datastore type if not set in config.
                # This can happen if the cluster is running with a version
                # of the k8s snap which doesn't copy the default datastore
                # type to the config.
                cluster = self._charm.api_manager.get_cluster_status().metadata
                datastore = cluster and cluster.status.datastore
                snap_ds = datastore and datastore.datastore_type
            opts.datastore = {v: k for k, v in DATASTORE_NAME_MAPPING.items()}.get(snap_ds)
        except (k8sd.K8sdConnectionError, k8sd.InvalidResponseError) as e:
            log.warning("Failed to load cluster config: %s", e)
            # Still return the options we have so far -- they will be validated later.

        return opts

    @property
    def config(self) -> ConfigOptions:
        """Return the current bootstrap configuration options."""
        immutable, juju = self.immutable, self._juju
        return ConfigOptions(
            datastore=immutable.datastore or juju.datastore,
            pod_cidr=immutable.pod_cidr or juju.pod_cidr,
            service_cidr=immutable.service_cidr or juju.service_cidr,
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
            if self._charm.is_worker:
                return  # Workers do not validate CIDRs.
            if cidr := config.service_cidr:
                valid_cidr(cidr or "", BOOTSTRAP_SERVICE_CIDR.name)
            if cidr := config.pod_cidr:
                valid_cidr(cidr, BOOTSTRAP_POD_CIDR.name)
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
        """Return the bootstrap configuration options from the juju config with auto-mapping.

        Options are always loaded from the charm config, or mapped through the default
        if they are set to "".
        """
        juju, empty = ConfigOptions(), ""
        if self._charm.is_control_plane:
            # Default to self-signed only if the charm is a control plane.
            if (val := BOOTSTRAP_DATASTORE.get(self._charm)) != empty:
                juju.datastore = val
            if (val := BOOTSTRAP_POD_CIDR.get(self._charm)) != empty:
                juju.pod_cidr = val
            if (val := BOOTSTRAP_SERVICE_CIDR.get(self._charm)) != empty:
                juju.service_cidr = val

        return juju


@context_status.on_error(
    ops.BlockedStatus("Invalid config on bootstrap-node-taints"), TypeError, ValueError
)
def node_taints(charm: ops.CharmBase) -> List[str]:
    """Share node taints with the kube-control interface."""
    taints = BOOTSTRAP_NODE_TAINTS.get(charm).split()
    for taint in taints:
        Taint.validate(taint)
    return taints
