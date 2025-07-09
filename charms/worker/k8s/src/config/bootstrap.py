# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import http
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
    DATASTORE_NAME_MAPPING,
)
from protocols import K8sCharmProtocol
from pydantic import BaseModel, Field

import charms.contextual_status as context_status
import charms.k8s.v0.k8sd_api_manager as k8sd_api_manager

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

    @property
    def user_status(self) -> ops.BlockedStatus:
        """Return a user-friendly block status."""
        return ops.BlockedStatus(f"Cannot config {self._name}='{self._new}'. Check logs")


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
        node_status: k8sd_api_manager.GetNodeStatusMetadata,
        cluster_config: Optional[k8sd_api_manager.GetClusterConfigMetadata] = None,
    ) -> "BootstrapConfigOptions":
        """Return a BootstrapCharmConfig instance from cluster config and node status.

        Args:
            cluster_config: The cluster configuration.
            node_status: The node status.

        Returns:
            A BootstrapConfigOptions instance with the configuration options.
        """
        # NOTE(Hue): certificates type should be determined based on the presence of the CA key.
        certificates = "self-signed" if pki.check_ca_key() else "external"

        datastore = (
            cluster_config and cluster_config.datastore and cluster_config.datastore.type or ""
        )

        return BootstrapConfigOptions(
            certificates=certificates,
            datastore=datastore,
            node_taints=" ".join(node_status.taints) if node_status.taints else "",
            pod_cidr=cluster_config and cluster_config.pod_cidr or "",
            service_cidr=cluster_config and cluster_config.service_cidr or "",
        )


@context_status.on_error(
    ops.WaitingStatus("Failed to get communicate with k8sd."),
    k8sd_api_manager.InvalidResponseError,
    k8sd_api_manager.K8sdConnectionError,
)
def detect_bootstrap_config_changes(charm: K8sCharmProtocol):
    """Prevent bootstrap config changes after bootstrap."""
    log.info("Preventing bootstrap config changes after bootstrap")

    try:
        ref_config = BootstrapConfigOptions.build(
            node_status=charm.api_manager.get_node_status().metadata,
            cluster_config=charm.api_manager.get_cluster_config().metadata
            if charm.is_control_plane
            else None,
        )
    except k8sd_api_manager.InvalidResponseError as e:
        if e.code == http.HTTPStatus.SERVICE_UNAVAILABLE:
            log.info("k8sd is not ready, skipping bootstrap config check")
            return
        raise

    if blocked := prevent(charm, ref_config):
        log.info("Bootstrap config changes are blocked: %s", blocked.message)
        context_status.add(blocked)
        raise context_status.ReconcilerError(blocked.message)


def prevent(
    charm: K8sCharmProtocol, ref_config: BootstrapConfigOptions
) -> Optional[ops.BlockedStatus]:
    """Prevent bootstrap config changes after bootstrap.

    Args:
        charm: The charm instance to check the bootstrap config options for.
        ref_config: The reference bootstrap config options to compare against.

    Returns:
        An ops.BlockedStatus if any bootstrap options have changed, None otherwise.
    """
    changes = []
    if charm.is_control_plane:
        changes += _prevent_control_plane_bootstrap_config_change(charm, ref_config)
    changes += _prevent_worker_bootstrap_config_change(charm, ref_config)
    return _prevent(changes)


def _prevent_control_plane_bootstrap_config_change(charm, ref: BootstrapConfigOptions):
    """Prevent control-plane bootstrap config changes after bootstrap.

    Args:
        charm: The charm instance to check the bootstrap config options for.
        ref: The reference bootstrap config options to compare against.

    Raises:
        BootstrapConfigChangeError: If any of the bootstrap config options have changed.
    """
    changes: List[ChangedConfig] = []

    datastore = BOOTSTRAP_DATASTORE.get(charm)
    certificates = BOOTSTRAP_CERTIFICATES.get(charm)
    pod_cidr = BOOTSTRAP_POD_CIDR.get(charm)
    service_cidr = BOOTSTRAP_SERVICE_CIDR.get(charm)

    # NOTE(Hue): We need a custom check here since the snap only knows
    # `dqlite` and `external` as datastores.
    # The charm config can be `dqlite`, `etcd`, etc.
    if _datastore_changed(datastore, ref.datastore):
        changes.append(
            ChangedConfig(
                name=BOOTSTRAP_DATASTORE.name,
                old=ref.datastore,
                new=datastore,
            )
        )

    if certificates != ref.certificates:
        changes.append(
            ChangedConfig(
                name=BOOTSTRAP_CERTIFICATES.name,
                old=ref.certificates,
                new=certificates,
            )
        )

    if pod_cidr != ref.pod_cidr:
        changes.append(
            ChangedConfig(
                name=BOOTSTRAP_POD_CIDR.name,
                old=ref.pod_cidr,
                new=pod_cidr,
            )
        )

    if service_cidr != ref.service_cidr:
        changes.append(
            ChangedConfig(
                name=BOOTSTRAP_SERVICE_CIDR.name,
                old=ref.service_cidr,
                new=service_cidr,
            )
        )
    return changes


def _prevent_worker_bootstrap_config_change(charm, ref: BootstrapConfigOptions):
    """Prevent worker bootstrap config changes after bootstrap.

    Args:
        charm: The charm instance to check the bootstrap config options for.
        ref: The reference bootstrap config options to compare against.

    Raises:
        BootstrapConfigChangeError: If any of the bootstrap config options have changed.
    """
    changes: List[ChangedConfig] = []

    taints = BOOTSTRAP_NODE_TAINTS.get(charm)

    if not _equal_taints(ref.node_taints, taints):
        changes.append(
            ChangedConfig(
                name=BOOTSTRAP_NODE_TAINTS.name,
                old=ref.node_taints,
                new=taints,
            )
        )
    return changes


def _prevent(changes: List[ChangedConfig]):
    """Prevent bootstrap config changes after bootstrap.

    Args:
        changes: A list of changed configuration options.

    Raises:
        BootstrapConfigChangeError: If any of the bootstrap config options have changed.
    """
    for c in changes:
        log.error("Bootstrap config '%s' should NOT be changed after bootstrap. %s", c._name, c)
    if changes:
        return changes[0].user_status


def _equal_taints(t1: str, t2: str) -> bool:
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


def _datastore_changed(charm_ds: str, snap_ds: str) -> bool:
    """Check if the datastore has changed.

    Args:
        charm_ds: The datastore in charm config.
        snap_ds: The datastore in snap cluster config.

    Returns:
        True if the datastore has changed, False otherwise.
    """
    # TODO(Hue): (KU-3226) Implement a mechanism to prevent changing external DBs.
    # Maybe stored state?

    return snap_ds != DATASTORE_NAME_MAPPING.get(charm_ds)
