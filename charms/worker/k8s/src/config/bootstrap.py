# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

import logging
from typing import List

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

from charms.k8s.v0.k8sd_api_manager import GetClusterConfigMetadata

log = logging.getLogger(__name__)


class ChangedConfig:
    """A class to represent the changed configuration."""

    def __init__(self, name, cluster_config, charm_config):
        """Create a new instance of the ChangedConfig class."""
        self.name = name
        self.cluster_config = cluster_config
        self.charm_config = charm_config

    def __str__(self) -> str:
        """Return a string representation of the ChangedConfig instance."""
        return f"{self.name}: {self.cluster_config=}, {self.charm_config=}"


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

    certificates: str = Field(
        description=f"Represents {CONFIG_BOOTSTRAP_CERTIFICATES} config option."
    )
    datastore: str = Field(description=f"Represents {CONFIG_BOOTSTRAP_DATASTORE} config option.")
    node_taints: str = Field(
        description=f"Represents {CONFIG_BOOTSTRAP_NODE_TAINTS} config option."
    )
    pod_cidr: str = Field(description=f"Represents {CONFIG_BOOTSTRAP_POD_CIDR} config option.")
    service_cidr: str = Field(
        description=f"Represents {CONFIG_BOOTSTRAP_SERVICE_CIDR} config option."
    )


def bootstrap_config_options_from_cluster_config(
    cluster_config: GetClusterConfigMetadata,
) -> BootstrapConfigOptions:
    """Return a BootstrapCharmConfig instance from the provided cluster config."""
    # NOTE(Hue): certificates type is not included in the cluster_config and should be
    # determined based on the presence of the CA key.
    certificates = "self-signed" if check_ca_key() else "external"

    datastore = (
        cluster_config.datastore.type
        if cluster_config.datastore and cluster_config.datastore.type
        else ""
    )
    # NOTE(Hue): datastore type `dqlite` (in charm) is equal to `k8s-dqlite` in the snap.
    # We change it to `dqlite` here to conform with the charm config.
    if datastore == "k8s-dqlite":
        datastore = "dqlite"

    node_taints = " ".join(cluster_config.node_taints) if cluster_config.node_taints else ""

    pod_cidr = (
        cluster_config.status.network.pod_cidr
        if cluster_config.status
        and cluster_config.status.network
        and cluster_config.status.network.pod_cidr
        else ""
    )

    service_cidr = (
        cluster_config.status.network.service_cidr
        if cluster_config.status
        and cluster_config.status.network
        and cluster_config.status.network.service_cidr
        else ""
    )

    return BootstrapConfigOptions(
        certificates=certificates,
        datastore=datastore,
        node_taints=node_taints,
        pod_cidr=pod_cidr,
        service_cidr=service_cidr,
    )


class BootstrapConfigChangePreventer:
    """Prevent bootstrap config changes after bootstrap."""

    def __init__(self, charm: K8sCharmProtocol):
        self._charm = charm

    def prevent(self, cluster_config: BootstrapConfigOptions):
        """Prevent bootstrap config changes after bootstrap."""
        if self._charm.is_control_plane:
            self._prevent_control_plane_bootstrap_config_change(cluster_config)
        else:
            self._prevent_worker_bootstrap_config_change(cluster_config)

    def _prevent_control_plane_bootstrap_config_change(self, ref: BootstrapConfigOptions):
        """Prevent control-plane bootstrap config changes after bootstrap.

        Raises:
            BootstrapConfigChangeError: If any of the bootstrap config options have changed.
        """
        changed: List[ChangedConfig] = []

        if self._charm.config.get(CONFIG_BOOTSTRAP_DATASTORE, "") != ref.datastore:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_DATASTORE,
                    cluster_config=ref.datastore,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_DATASTORE, ""),
                )
            )

        if self._charm.config.get(CONFIG_BOOTSTRAP_CERTIFICATES, "") != ref.certificates:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_CERTIFICATES,
                    cluster_config=ref.certificates,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_CERTIFICATES, ""),
                )
            )

        if self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, "") != ref.node_taints:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_NODE_TAINTS,
                    cluster_config=ref.node_taints,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, ""),
                )
            )

        if self._charm.config.get(CONFIG_BOOTSTRAP_POD_CIDR, "") != ref.pod_cidr:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_POD_CIDR,
                    cluster_config=ref.pod_cidr,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_POD_CIDR, ""),
                )
            )

        if self._charm.config.get(CONFIG_BOOTSTRAP_SERVICE_CIDR, "") != ref.service_cidr:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_SERVICE_CIDR,
                    cluster_config=ref.service_cidr,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_SERVICE_CIDR, ""),
                )
            )

        if changed:
            for c in changed:
                log.error(
                    "Bootstrap config '%s' should NOT be changed after bootstrap. %s", c.name, c
                )
            raise BootstrapConfigChangeError(changed)

    def _prevent_worker_bootstrap_config_change(self, ref: BootstrapConfigOptions):
        """Prevent worker bootstrap config changes after bootstrap.

        Raises:
            BootstrapConfigChangeError: If any of the bootstrap config options have changed.
        """
        changed: List[ChangedConfig] = []

        if self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, "") != ref.node_taints:
            changed.append(
                ChangedConfig(
                    name=CONFIG_BOOTSTRAP_NODE_TAINTS,
                    cluster_config=ref.node_taints,
                    charm_config=self._charm.config.get(CONFIG_BOOTSTRAP_NODE_TAINTS, ""),
                )
            )

        if changed:
            for c in changed:
                log.error(
                    "Bootstrap config '%s' should NOT be changed after bootstrap. %s", c.name, c
                )
            raise BootstrapConfigChangeError(changed)
