# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Cloud Integration for Canonical k8s Operator."""

import logging
from typing import Mapping, Optional, Union

import charms.contextual_status as status
import ops
from ops.interface_aws.requires import AWSIntegrationRequires
from ops.interface_azure.requires import AzureIntegrationRequires
from ops.interface_gcp.requires import GCPIntegrationRequires
from protocols import K8sCharmBase

log = logging.getLogger(__name__)

CloudSpecificIntegration = Union[
    AWSIntegrationRequires, AzureIntegrationRequires, GCPIntegrationRequires
]


class CloudIntegration:
    """Utility class that handles the integration with clouds for Canonical k8s.

    This class provides methods to configure instance tags and roles for control-plane
    units

    Attributes:
        charm (K8sCharm): Reference to the base charm instance.
        cloud (CloudSpecificIntegration): Reference to the cloud-specific integration.
    """

    def __init__(self, charm: K8sCharmBase, is_control_plane: bool) -> None:
        """Integrate with all possible clouds.

        Args:
            charm (K8sCharm): Reference to the base charm instance.
            is_control_plane (bool): Flag to determine if the unit is a control-plane node.
        """
        self.charm = charm
        self.is_control_plane = is_control_plane
        self.cloud_support: Mapping[str, CloudSpecificIntegration] = {
            "aws": AWSIntegrationRequires(charm),
            "gce": GCPIntegrationRequires(charm),
            "azure": AzureIntegrationRequires(charm),
        }

    @property
    def cloud(self) -> Optional[CloudSpecificIntegration]:
        """Determine if we're integrated with a known cloud."""
        cloud_name = self.charm.get_cloud_name()
        if not (cloud := self.cloud_support.get(cloud_name)):
            log.warning("Skipping direct cloud integration: cloud %s", cloud_name)
            return None

        if not cloud.relation:
            log.info(
                "Skipping Cloud integration: Needs an active %s relation to integrate.", cloud_name
            )
            return None
        return cloud

    def _integrate_aws(self, cloud: AWSIntegrationRequires, cluster_tag: str):
        """Integrate with AWS cloud.

        Args:
            cloud (AWSIntegrationRequires): AWS cloud integration.
            cluster_tag (str): Tag to identify the cluster.
        """
        aws_cluster_tag = {f"kubernetes.io/cluster/{cluster_tag}": "owned"}
        if self.is_control_plane:
            # wokeignore:rule=master
            cloud.tag_instance({**aws_cluster_tag, "k8s.io/role/master": "true"})
            cloud.tag_instance_security_group(aws_cluster_tag)
            cloud.tag_instance_subnet(aws_cluster_tag)
            cloud.enable_object_storage_management(["kubernetes-*"])
            cloud.enable_load_balancer_management()

            # Necessary for cloud-provider-aws
            cloud.enable_autoscaling_readonly()
            cloud.enable_instance_modification()
            cloud.enable_region_readonly()
        else:
            cloud.tag_instance(aws_cluster_tag)
            cloud.tag_instance_security_group(aws_cluster_tag)
            cloud.tag_instance_subnet(aws_cluster_tag)
            cloud.enable_object_storage_management(["kubernetes-*"])

    def _integrate_gcp(self, cloud: GCPIntegrationRequires, cluster_tag: str):
        """Integrate with GCP cloud.

        Args:
            cloud (GCPIntegrationRequires): GCP cloud integration.
            cluster_tag (str): Tag to identify the cluster.
        """
        gcp_cluster_tag = {"k8s-io-cluster-name": cluster_tag}
        if self.is_control_plane:
            # wokeignore:rule=master
            cloud.tag_instance({**gcp_cluster_tag, "k8s-io-role-master": "master"})
            cloud.enable_object_storage_management()
            cloud.enable_security_management()
        else:
            cloud.tag_instance(gcp_cluster_tag)
            cloud.enable_object_storage_management()

    def _integrate_azure(self, cloud: AzureIntegrationRequires, cluster_tag: str):
        """Integrate with Azure cloud.

        Args:
            cloud (AzureIntegrationRequires): Azure cloud integration.
            cluster_tag (str): Tag to identify the cluster.
        """
        azure_cluster_tag = {"k8s-io-cluster-name": cluster_tag}
        if self.is_control_plane:
            # wokeignore:rule=master
            cloud.tag_instance({**azure_cluster_tag, "k8s-io-role-master": "master"})
            cloud.enable_object_storage_management()
            cloud.enable_security_management()
            cloud.enable_loadbalancer_management()
        else:
            cloud.tag_instance(azure_cluster_tag)
            cloud.enable_object_storage_management()

    @status.on_error(ops.WaitingStatus("Waiting for cloud-integration"))
    def integrate(self, cluster_tag: str, event: ops.EventBase):
        """Request tags and permissions for a control-plane node.

        Args:
            cluster_tag (str):     Tag to identify the integrating cluster.
            event (ops.EventBase): Event that triggered the integration

        Raises:
            ValueError: If the cloud integration evaluation fails
        """
        if not (cloud := self.cloud):
            return

        if not cluster_tag:
            raise ValueError("Cluster-tag is required for cloud integration")

        cloud_name = self.charm.get_cloud_name()

        status.add(ops.MaintenanceStatus(f"Integrate with {cloud_name}"))
        if isinstance(cloud, AWSIntegrationRequires):
            self._integrate_aws(cloud, cluster_tag)
        elif isinstance(cloud, GCPIntegrationRequires):
            self._integrate_gcp(cloud, cluster_tag)
        elif isinstance(cloud, AzureIntegrationRequires):
            self._integrate_azure(cloud, cluster_tag)
        cloud.enable_instance_inspection()
        cloud.enable_dns_management()
        if self.is_control_plane:
            cloud.enable_network_management()
            cloud.enable_block_storage_management()
        errors = cloud.evaluate_relation(event)
        if errors:
            log.error("Failed to evaluate cloud integration: %s", errors)
            raise ValueError("Failed to evaluate cloud integration")
