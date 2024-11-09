# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Cloud Integration for Charmed Kubernetes Control Plane."""

import logging
from typing import Optional, Union

import charms.contextual_status as status
import ops
from ops.interface_aws.requires import AWSIntegrationRequires
from ops.interface_azure.requires import AzureIntegrationRequires
from ops.interface_gcp.requires import GCPIntegrationRequires
from protocols import K8sCharmProtocol

log = logging.getLogger(__name__)

CloudSpecificIntegration = Union[
    AWSIntegrationRequires, AzureIntegrationRequires, GCPIntegrationRequires
]


class CloudIntegration:
    """Utility class that handles the integration with clouds for Charmed Kubernetes.

    This class provides methods to configure instance tags and roles for control-plane
    units

    Attributes:
        charm (K8sCharm): Reference to the base charm instance.
        cloud (CloudSpecificIntegration): Reference to the cloud-specific integration.
    """

    def __init__(self, charm: K8sCharmProtocol, is_control_plane: bool) -> None:
        """Integrate with all possible clouds.

        Args:
            charm (K8sCharm): Reference to the base charm instance.
            is_control_plane (bool): Flag to determine if the unit is a control-plane node.
        """
        self.charm = charm
        self.is_control_plane = is_control_plane

    @property
    def cloud(self) -> Optional[CloudSpecificIntegration]:
        """Determine if we're integrated with a known cloud."""
        cloud_name = self.charm.get_cloud_name()
        cloud: CloudSpecificIntegration
        if cloud_name == "aws":
            cloud = AWSIntegrationRequires(self.charm)
        elif cloud_name == "gcp":
            cloud = GCPIntegrationRequires(self.charm)
        elif cloud_name == "azure":
            cloud = AzureIntegrationRequires(self.charm)
        else:
            log.warning("Skipping direct cloud integration: cloud %s", cloud_name)
            return None

        if not cloud.relation:
            log.info(
                "Skipping Cloud integration: Needs an active %s relation to integrate.", cloud_name
            )
            return None
        return cloud

    @status.on_error(ops.WaitingStatus("Waiting for cloud-integration"))
    def integrate(self, event: ops.EventBase):
        """Request tags and permissions for a control-plane node.

        Args:
            event (ops.EventBase): Event that triggered the integration

        Raises:
            ValueError: If the cloud integration evaluation fails
        """
        if not (cloud := self.cloud):
            return

        cloud_name = self.charm.get_cloud_name()
        cluster_tag = self.charm.get_cluster_name()

        status.add(ops.MaintenanceStatus(f"Integrate with {cloud_name}"))
        if isinstance(cloud, AWSIntegrationRequires):
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
        elif isinstance(cloud, GCPIntegrationRequires):
            gcp_cluster_tag = {"k8s-io-cluster-name": cluster_tag}
            if self.is_control_plane:
                # wokeignore:rule=master
                cloud.tag_instance({**gcp_cluster_tag, "k8s-io-role-master": "master"})
                cloud.enable_object_storage_management()
                cloud.enable_security_management()
            else:
                cloud.tag_instance(gcp_cluster_tag)
                cloud.enable_object_storage_management()
        elif isinstance(cloud, AzureIntegrationRequires):
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
        cloud.enable_instance_inspection()
        cloud.enable_dns_management()
        if self.is_control_plane:
            cloud.enable_network_management()
            cloud.enable_block_storage_management()
        evaluation = cloud.evaluate_relation(event)
        if not evaluation:
            log.error("Failed to evaluate cloud integration: %s", evaluation)
            raise ValueError("Failed to evaluate cloud integration")
