# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Protocol definitions module."""

from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

import ops
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.k8s.v0.k8sd_api_manager import K8sdAPIManager
from charms.reconciler import Reconciler
from config.resource import CharmResource
from inspector import ClusterInspector
from ops.interface_kube_control import KubeControlProvides


class CanUpgrade:
    """Protocol for upgrade functionality in K8sCharm.

    Attributes:
        upgrade_granted (bool): Indicates if the upgrade has been granted.
    """

    upgrade_granted: bool


class K8sCharmProtocol(ops.CharmBase):
    """Typing for the K8sCharm.

    Attributes:
        api_manager (K8sdAPIManager): The API manager for the charm.
        cluster_inspector (ClusterInspector): The cluster inspector for the charm.
        kube_control (KubeControlProvides): The kube-control interface.
        xcp (ExternalCloudProvider): The external cloud provider interface.
        reconciler (Reconciler): The reconciler for the charm
        lead_control_plane (bool): Whether the charm is the lead control plane.
        is_control_plane (bool): Whether the charm is a control plane.
        is_worker (bool): Whether the charm is a worker.
    """

    api_manager: K8sdAPIManager
    cluster_inspector: ClusterInspector
    kube_control: KubeControlProvides
    kubeconfig: Path
    xcp: ExternalCloudProvider
    snap_installation_resource: CharmResource
    reconciler: Reconciler
    lead_control_plane: bool
    upgrade: CanUpgrade
    is_control_plane: bool
    is_worker: bool

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

    def get_node_name(self) -> str:
        """Return the lowercase hostname.

        Returns:
            the hostname of the machine.
        """
        raise NotImplementedError

    def get_worker_versions(self) -> Dict[str, List[ops.Unit]]:
        """Get the worker versions.

        Raises:
            NotImplementedError: If the method is not implemented.
        """
        raise NotImplementedError

    def split_sans_by_type(self) -> Tuple[FrozenSet[str], FrozenSet[str]]:
        """Split SANs into IP addresses and DNS names.

        Returns:
            Tuple[FrozenSet[str], FrozenSet[str]]: IP addresses and DNS names.
        """
        raise NotImplementedError
