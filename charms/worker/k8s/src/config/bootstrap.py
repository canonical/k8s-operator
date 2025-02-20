# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Bootstrap configuration options."""

from literals import (
    CONFIG_BOOTSTRAP_CERTIFICATES,
    CONFIG_BOOTSTRAP_DATASTORE,
    CONFIG_BOOTSTRAP_NODE_TAINTS,
    CONFIG_BOOTSTRAP_POD_CIDR,
    CONFIG_BOOTSTRAP_SERVICE_CIDR,
)
from pydantic import BaseModel, Field

from charms.k8s.v0.k8sd_api_manager import GetClusterConfigMetadata


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
    datastore = (
        cluster_config.datastore.type
        if cluster_config.datastore and cluster_config.datastore.type
        else ""
    )

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
        # NOTE(Hue) bootstrap-certificates can not be obtained from the cluster config as it's
        # not directly represented in the cluster config.
        certificates="",
        datastore=datastore,
        node_taints=node_taints,
        pod_cidr=pod_cidr,
        service_cidr=service_cidr,
    )
