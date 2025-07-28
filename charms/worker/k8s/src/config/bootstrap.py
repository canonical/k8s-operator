# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import http
import logging
from collections import Counter
from typing import Optional

import ops
from config.option import CharmOption
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


class ConfigComparison:
    """A class to represent a changed configuration."""

    def __init__(
        self, charm: ops.CharmBase, option: CharmOption, current, eq=None, mapping=None
    ) -> None:
        """Initialize the ConfigComparison instance.

        Args:
            charm: The charm instance to fetch the current config
            option: The charm option to compare against.
            current: The current value of the configuration option from the snap.
            eq: A function to compare the current and configured values. Defaults to equality.
            mapping: A mapping of charm values to snap values.
        """
        self._charm = charm
        self._option = option
        self._mapping = mapping or {}
        # The equality function to compare the charm and current values
        self._eq = eq or (lambda x, y: x == y)
        # Represents the model's config value
        self._charm_val = self._option.get(self._charm)
        # Represents the charm config which would set this in the snap
        self._cur_val = self._unmapped(current)

    def _unmapped(self, snap_value: str) -> str:
        """Return the charm value for the given snap value."""
        return {v: k for k, v in self._mapping.items()}.get(snap_value, snap_value)

    @property
    def matching(self) -> bool:
        """Check if the configuration has matches."""
        if not (matches := self._eq(self._cur_val, self._charm_val)):
            log.warning(
                "Cannot satisfy configuration %s='%s'. Run `juju config %s %s='%s'`",
                self._option.name,
                self._charm_val,
                self._charm.app.name,
                self._option.name,
                self._cur_val,
            )
        return matches

    @property
    def user_status(self) -> ops.BlockedStatus:
        """Return a user-friendly block status."""
        return ops.BlockedStatus(
            f"Expected {self._option.name}='{self._cur_val}' not '{self._charm_val}'"
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
        certificate_provider: str,
        node_status: k8sd_api_manager.GetNodeStatusMetadata,
        cluster_config: Optional[k8sd_api_manager.GetClusterConfigMetadata] = None,
    ) -> "BootstrapConfigOptions":
        """Return a BootstrapCharmConfig instance from cluster config and node status.

        Args:
            certificate_provider: The certificate provider to use.
            cluster_config: The cluster configuration.
            node_status: The node status.

        Returns:
            A BootstrapConfigOptions instance with the configuration options.
        """
        return BootstrapConfigOptions(
            certificates=certificate_provider,
            datastore=cluster_config
            and cluster_config.datastore
            and cluster_config.datastore.type
            or "",
            node_taints=" ".join(node_status.taints) if node_status.taints else "",
            pod_cidr=cluster_config and cluster_config.pod_cidr or "",
            service_cidr=cluster_config and cluster_config.service_cidr or "",
        )

    def prevent(self, charm: K8sCharmProtocol) -> Optional[ops.BlockedStatus]:
        """Prevent bootstrap config changes after bootstrap.

        Args:
            charm: The charm instance to check the bootstrap config options for.
            ref_config: The reference bootstrap config options to compare against.

        Returns:
            An ops.BlockedStatus if any bootstrap options have changed, None otherwise.
        """
        test = []
        if charm.is_control_plane:
            test.append(
                ConfigComparison(
                    charm,
                    BOOTSTRAP_DATASTORE,
                    self.datastore,
                    mapping=DATASTORE_NAME_MAPPING,
                )
            )
            test.append(ConfigComparison(charm, BOOTSTRAP_CERTIFICATES, self.certificates))
            test.append(ConfigComparison(charm, BOOTSTRAP_POD_CIDR, self.pod_cidr))
            test.append(ConfigComparison(charm, BOOTSTRAP_SERVICE_CIDR, self.service_cidr))
        test += [
            ConfigComparison(charm, BOOTSTRAP_NODE_TAINTS, self.node_taints, eq=_equal_taints)
        ]

        if reports := [c for c in test if not c.matching]:
            return reports[0].user_status
        return None


def _equal_taints(t1: str, t2: str) -> bool:
    """Check if two taint strings are equal.

    The strings can be in any order and individual taints can be separated by spaces.

    Args:
        t1: The first taint string.
        t2: The second taint string.

    Returns:
        True if the two taint strings are equal, False otherwise.
    """
    return Counter(t1.split()) == Counter(t2.split())


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
            certificate_provider=charm.certificates.get_provider_name(),
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

    if blocked := ref_config.prevent(charm):
        context_status.add(blocked)
        raise context_status.ReconcilerError(blocked.message)
