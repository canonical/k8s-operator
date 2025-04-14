# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import logging
from typing import List, Optional

from literals import (
    CONFIG_BOOTSTRAP_CERTIFICATES,
    CONFIG_BOOTSTRAP_DATASTORE,
    CONFIG_BOOTSTRAP_NODE_TAINTS,
    CONFIG_BOOTSTRAP_POD_CIDR,
    CONFIG_BOOTSTRAP_SERVICE_CIDR,
)
from pki import check_ca_key
from protocols import K8sCharmProtocol
from pydantic import BaseModel, Field

from charms.k8s.v0.k8sd_api_manager import GetClusterConfigMetadata, GetNodeStatusMetadata

log = logging.getLogger(__name__)


class ChangedConfig:
    """A class to represent a changed configuration."""

    def __init__(self, name: str, old, new) -> None:
        """Create a new instance of the ChangedConfig class."""
        self._name = name
        self._old = old
        self._new = new

    def __str__(self) -> str:
        """Return a string representation of the ChangedConfig instance."""
        return f"{self._name}: {self._old=}, {self._new=}"


class BootstrapConfigChangeError(Exception):
    """An exception raised when a bootstrap config is changed."""

    def __init__(self, changed: List[ChangedConfig]):
        self.changed = changed
        self._changed_str = "\n".join(str(c) for c in changed)
        super().__init__(
            "Bootstrap config options can not be changed. "
            f"Change the charm config options to match the cluster config:\n{self._changed_str}"
        )


class BootstrapConfigOptions(BaseModel):
    """Charm config options that has a `bootstrap-` prefix."""

    certificates: str = Field()
    datastore: str = Field()
    node_taints: str = Field()
    pod_cidr: str = Field()
    service_cidr: str = Field()

    @classmethod
    def build(
        cls,
        node_status: GetNodeStatusMetadata,
        cluster_config: Optional[GetClusterConfigMetadata] = None,
    ) -> "BootstrapConfigOptions":
        """Return a BootstrapCharmConfig instance from cluster config and node status.

        Args:
            cluster_config: The cluster configuration.
            node_status: The node status.

        Returns:
            A BootstrapConfigOptions instance with the configuration options.
        """
        # NOTE(Hue): certificates type should be determined based on the presence of the CA key.
        certificates = "self-signed" if check_ca_key() else "external"

        datastore = (
            cluster_config and cluster_config.datastore and cluster_config.datastore.type or ""
        )
        # NOTE(Hue): datastore type `dqlite` (in charm) is equal to `k8s-dqlite` in the snap.
        # We change it to `dqlite` here to conform with the charm config.
        if datastore == "k8s-dqlite":
            datastore = "dqlite"

        return BootstrapConfigOptions(
            certificates=certificates,
            datastore=datastore,
            node_taints=" ".join(node_status.taints) if node_status.taints else "",
            pod_cidr=cluster_config and cluster_config.pod_cidr or "",
            service_cidr=cluster_config and cluster_config.service_cidr or "",
        )


class BootstrapConfigChangePreventer:
    """Prevent bootstrap config changes after bootstrap."""

    def __init__(self, charm: K8sCharmProtocol):
        self._charm = charm

    def prevent(self, ref_config: BootstrapConfigOptions):
        """Prevent bootstrap config changes after bootstrap.

        Args:
            ref_config: The reference bootstrap config options to compare against.
        """
        if self._charm.is_control_plane:
            self._prevent_control_plane_bootstrap_config_change(ref_config)
        else:
            self._prevent_worker_bootstrap_config_change(ref_config)

    def _prevent_control_plane_bootstrap_config_change(self, ref: BootstrapConfigOptions):
        """Prevent control-plane bootstrap config changes after bootstrap.

        Args:
            ref: The reference bootstrap config options to compare against.

        Raises:
            BootstrapConfigChangeError: If any of the bootstrap config options have changed.
        """
        changed: List[ChangedConfig] = []

        datastore = self._charm.config.get(CONFIG_BOOTSTRAP_DATASTORE, "")
        certificates = self._charm.config.get(CONFIG_BOOTSTRAP_CERTIFICATES, "")
        taints = self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, "")
        pod_cidr = self._charm.config.get(CONFIG_BOOTSTRAP_POD_CIDR, "")
        service_cidr = self._charm.config.get(CONFIG_BOOTSTRAP_SERVICE_CIDR, "")

        # NOTE(Hue): We need a custom check here since the snap only knows 
        # `dqlite` and `external` as datastores.
        # The charm config can be `dqlite`, `etcd`, etc.
        if datastore_changed(str(datastore), ref.datastore):
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_DATASTORE,
                    old=ref.datastore,
                    new=datastore,
                )
            )

        if certificates != ref.certificates:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_CERTIFICATES,
                    old=ref.certificates,
                    new=certificates,
                )
            )

        if not self._equal_taints(ref.node_taints, str(taints)):
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_NODE_TAINTS,
                    old=ref.node_taints,
                    new=taints,
                )
            )

        if pod_cidr != ref.pod_cidr:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_POD_CIDR,
                    old=ref.pod_cidr,
                    new=pod_cidr,
                )
            )

        if service_cidr != ref.service_cidr:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_SERVICE_CIDR,
                    old=ref.service_cidr,
                    new=service_cidr,
                )
            )

        self._prevent(changed)

    def _prevent_worker_bootstrap_config_change(self, ref: BootstrapConfigOptions):
        """Prevent worker bootstrap config changes after bootstrap.

        Args:
            ref: The reference bootstrap config options to compare against.

        Raises:
            BootstrapConfigChangeError: If any of the bootstrap config options have changed.
        """
        changed: List[ChangedConfig] = []

        taints = self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, "")

        if not self._equal_taints(ref.node_taints, str(taints)):
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_NODE_TAINTS,
                    old=ref.node_taints,
                    new=taints,
                )
            )

        self._prevent(changed)

    def _prevent(self, changes: List[ChangedConfig]):
        """Prevent bootstrap config changes after bootstrap.

        Args:
            changes: A list of changed configuration options.

        Raises:
            BootstrapConfigChangeError: If any of the bootstrap config options have changed.
        """
        if changes:
            for c in changes:
                log.error(
                    "Bootstrap config '%s' should NOT be changed after bootstrap. %s", c._name, c
                )
            raise BootstrapConfigChangeError(changes)

    def _equal_taints(self, t1: str, t2: str) -> bool:
        """Check if two taint strings are equal.

        The strings can be in any order and individual taints can be separated by spaces.

        Args:
            t1: The first taint string.
            t2: The second taint string.

        Returns:
            True if the two taint strings are equal, False otherwise.
        """
        t1_split, t2_split = t1.split(), t2.split()
        return len(t1_split) == len(t2_split) and set(t1_split) == set(t2_split)


def datastore_changed(charm_ds: str, snap_ds: str) -> bool:
    """Check if the datastore has changed.

    Args:
        charm_ds: The datastore in charm config.
        snap_ds: The datastore in snap cluster config.

    Returns:
        True if the datastore has changed, False otherwise.
    """
    # TODO(Hue): (KU-3226) Implement a mechanism to prevent changing external DBs. Maybe stored state?
    if charm_ds != "dqlite" and snap_ds == "external":
        return True

    return charm_ds == snap_ds
