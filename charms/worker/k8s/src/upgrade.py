"""A module for upgrading the k8s and k8s-worker charms."""

import logging
from typing import List

from charms.data_platform_libs.v0.upgrade import ClusterNotReadyError, DataUpgrade, DependencyModel
from inspector import ClusterInspector
from pydantic import BaseModel

log = logging.getLogger(__name__)


class K8sDependenciesModel(BaseModel):
    """A model for the k8s and k8s-worker charm dependencies.

    Attributes:
        k8s_charm: The k8s charm dependency model.
        k8s_service: The k8s-service charm dependency model.
    """

    k8s_charm: DependencyModel
    k8s_service: DependencyModel


class K8sUpgrade(DataUpgrade):
    """A helper class for upgrading the k8s and k8s-worker charms."""

    def __init__(self, charm, node_manager: ClusterInspector, **kwargs):
        """Initialize the K8sUpgrade.

        Args:
            charm: The charm instance.
            node_manager: The ClusterInspector instance.
            kwargs: Additional keyword arguments.
        """
        super().__init__(charm, **kwargs)
        self.charm = charm
        self.node_manager = node_manager

    def pre_upgrade_check(self) -> None:
        """Check if the cluster is ready for an upgrade.

        It verifies that the cluster nodes are ready before proceeding and
        if the pods in the specified namespace are ready.

        Raises:
            ClusterNotReadyError: If the cluster is not ready for an upgrade.
        """
        try:
            nodes = self.node_manager.get_nodes(
                label_selector={"juju-charm": "k8s-worker" if self.charm.is_worker else "k8s"}
            )
        except ClusterInspector.ClusterInspectorError as e:
            raise ClusterNotReadyError(
                "Cluster is not ready",
                str(e),
                "API server may not be ready",
            ) from e

        nodes = nodes or []
        unready_nodes = [node.name for node in nodes if node.status != "Ready"]

        if unready_nodes:
            raise ClusterNotReadyError(
                "Cluster is not ready",
                f"Nodes not ready: {', '.join(unready_nodes)}",
                "Node(s) may be in a bad state",
            )

        if failing_pods := self.node_manager.verify_pods_running(["kube-system"], timeout=60):
            raise ClusterNotReadyError(
                "Cluster is not ready",
                f"Pods not running in namespace(s): {failing_pods}",
                "Pods may be in a bad state",
            )

    def build_upgrade_stack(self) -> List[int]:
        """Return a list of unit numbers to upgrade in order.

        Returns:
            A list of unit numbers to upgrade in order.
        """
        relation = self.charm.model.get_relation("cluster")
        if not relation:
            return [int(self.charm.unit.name.split("/")[-1])]

        return [
            int(unit.name.split("/")[-1]) for unit in ({self.charm.unit} | set(relation.units))
        ]

    def log_rollback_instructions(self) -> None:
        """Log instructions for rolling back the upgrade."""
        log.critical(
            "To rollback the upgrade, run: `juju refresh` to the previously deployed revision."
        )
