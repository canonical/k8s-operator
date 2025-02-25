# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Protocol definitions module."""

import ops
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager
from charms.reconciler import Reconciler
from ops.interface_kube_control import KubeControlProvides


class K8sCharmProtocol(ops.CharmBase):
    """Typing for the K8sCharm.

    Attributes:
        api_manager (K8sdAPIManager): The API manager for the charm.
        kube_control (KubeControlProvides): The kube-control interface.
        xcp (ExternalCloudProvider): The external cloud provider interface.
        reconciler (Reconciler): The reconciler for the charm
    """

    api_manager: K8sdAPIManager
    kube_control: KubeControlProvides
    xcp: ExternalCloudProvider
    reconciler: Reconciler

    def get_cluster_name(self) -> str:
        """Get the cluster name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    def get_cloud_name(self) -> str:
        """Get the cloud name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    def _is_node_ready(self) -> bool:
        """Check if the node is ready.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
