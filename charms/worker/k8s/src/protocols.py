# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Protocol definitions module."""

from typing import Dict, List

import ops
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager
from charms.reconciler import Reconciler
from inspector import ClusterInspector
from ops.interface_kube_control import KubeControlProvides


class K8sCharmProtocol(ops.CharmBase):
    """Typing for the K8sCharm.

    Attributes:
        api_manager (K8sdAPIManager): The API manager for the charm.
        cluster_inspector (ClusterInspector): The cluster inspector for the charm.
        kube_control (KubeControlProvides): The kube-control interface.
        xcp (ExternalCloudProvider): The external cloud provider interface.
        reconciler (Reconciler): The reconciler for the charm
        is_upgrade_granted (bool): Whether the upgrade is granted.
        lead_control_plane (bool): Whether the charm is the lead control plane.
        is_control_plane (bool): Whether the charm is a control plane.
        is_worker (bool): Whether the charm is a worker.
        datastore (str): The datastore for Kubernetes.
    """

    api_manager: K8sdAPIManager
    cluster_inspector: ClusterInspector
    kube_control: KubeControlProvides
    xcp: ExternalCloudProvider
    reconciler: Reconciler
    is_upgrade_granted: bool
    lead_control_plane: bool
    is_control_plane: bool
    is_worker: bool
    datastore: str

    def get_cluster_name(self) -> str:
        """Get the cluster name.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    def grant_upgrade(self) -> None:
        """Grant the upgrade.

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

    def get_worker_versions(self) -> Dict[str, List[ops.Unit]]:
        """Get the worker versions.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError
