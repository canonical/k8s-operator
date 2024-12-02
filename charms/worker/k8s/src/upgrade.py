# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""A module for upgrading the k8s and k8s-worker charms."""

import logging
from functools import wraps
from typing import List, Union

import charms.contextual_status as status
import ops
import reschedule
from charms.data_platform_libs.v0.upgrade import (
    ClusterNotReadyError,
    DataUpgrade,
    DependencyModel,
    UpgradeGrantedEvent,
    verify_requirements,
)
from charms.operator_libs_linux.v2.snap import SnapError
from inspector import ClusterInspector
from literals import K8S_COMMON_SERVICES, K8S_CONTROL_PLANE_SERVICES, SNAP_NAME
from protocols import K8sCharmProtocol
from pydantic import BaseModel
from snap import management as snap_management
from snap import start, stop
from snap import version as snap_version

log = logging.getLogger(__name__)


def reset_snap_upgrade(method):
    """Decorate a method to reset the snap upgrade status.

    Args:
        method: The method to decorate.

    Returns:
        The decorated method.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        """Reset the snap upgrade status.

        Args:
            args: Additional arguments.
            kwargs: Additional keyword arguments.

        Returns:
            The method result.
        """
        try:
            return method(self, *args, **kwargs)
        finally:
            self.charm.reset_upgrade()

    return wrapper


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

    def __init__(self, charm: K8sCharmProtocol, node_manager: ClusterInspector, **kwargs):
        """Initialize the K8sUpgrade.

        Args:
            charm: The charm instance.
            node_manager: The ClusterInspector instance.
            kwargs: Additional keyword arguments.
        """
        super().__init__(charm, **kwargs)
        self.charm = charm
        self.node_manager = node_manager

    def set_upgrade_status(self, event: ops.UpdateStatusEvent) -> None:
        """Set the Juju upgrade status.

        Args:
            event: The UpdateStatusEvent instance.
        """
        upgrade_status = self.state
        if not upgrade_status:
            return
        if upgrade_status == "upgrading":
            if not self.charm.is_upgrade_granted:
                self._upgrade(event)
        elif upgrade_status == "recovery":
            status.add(ops.MaintenanceStatus("Charm is in recovery mode. Please check the logs."))
            return
        elif upgrade_status == "failed":
            status.add(ops.BlockedStatus("Upgrade Failed. Please check the logs."))
            return

    def pre_upgrade_check(self) -> None:
        """Check if the cluster is ready for an upgrade.

        It verifies that the cluster nodes are ready before proceeding and
        if the pods in the specified namespace are ready.

        Raises:
            ClusterNotReadyError: If the cluster is not ready for an upgrade.
        """
        try:
            nodes = self.node_manager.get_nodes(
                labels={"juju-charm": "k8s-worker" if self.charm.is_worker else "k8s"}
            )
            failing_pods = self.node_manager.verify_pods_running(["kube-system"])
        except ClusterInspector.ClusterInspectorError as e:
            raise ClusterNotReadyError(
                message="Cluster is not ready for an upgrade",
                cause=str(e),
                resolution="""API server may not be reachable.
                Please check that the API server is up and running.""",
            ) from e

        unready_nodes = nodes or []

        if unready_nodes:
            raise ClusterNotReadyError(
                message="Cluster is not ready for an upgrade",
                cause=f"Nodes not ready: {', '.join(node.metadata.name for node in unready_nodes)}",
                resolution="""Node(s) may be in a bad state.
                    Please check the node(s) for more information.""",
            )

        if failing_pods:
            raise ClusterNotReadyError(
                message="Cluster is not ready",
                cause=f"Pods not running in namespace(s): {failing_pods}",
                resolution="Check the logs for the failing pods.",
            )

    def _verify_worker_version(self, current_version: str) -> bool:
        """Verify the worker version.

        Args:
            current_version: The current version of the k8s snap.

        Returns:
            True if the worker version meets the requirements; False otherwise.
        """
        worker_version = self.charm.get_worker_version()
        if not worker_version:
            return True
        dependency_model: DependencyModel = getattr(self.dependency_model, "k8s_service")

        if not verify_requirements(version=current_version, requirement=worker_version):
            log.error(
                """The k8s worker charm version does not meet the requirements.
                Version installed: %s, Supported versions: %s
                """,
                worker_version,
                dependency_model.dependencies["k8s-worker"],
            )
            return False

        return True

    def _perform_upgrade(self, services: List[str]) -> None:
        """Perform the upgrade.

        Args:
            services: The services to stop and start during the upgrade.
        """
        self.charm.unit.status = ops.MaintenanceStatus("Stopping k8s Services.")
        stop(SNAP_NAME, services=services)
        self.charm.unit.status = ops.MaintenanceStatus("Upgrading the k8s snap.")
        snap_management(self.charm)
        self.charm.unit.status = ops.MaintenanceStatus("Starting k8s Services.")
        start(SNAP_NAME, services=services)

    def _on_upgrade_granted(self, event: UpgradeGrantedEvent) -> None:
        """Handle the upgrade granted event.

        Args:
            event: The UpgradeGrantedEvent instance.
        """
        with status.context(self.charm.unit, exit_status=ops.ActiveStatus("Ready")):
            self._upgrade(event)

    @reset_snap_upgrade
    def _upgrade(self, event: Union[ops.EventBase, ops.HookEvent]) -> None:
        """Upgrade the snap workload."""
        trigger = reschedule.PeriodicEvent(self.charm)
        current_version, _ = snap_version("k8s")

        if not current_version:
            log.error("Failed to get the version of the k8s snap.")
            self.set_unit_failed(cause="Failed to get the version of the k8s snap.")
            return

        self.charm.unit.status = ops.MaintenanceStatus("Upgrading the charm.")

        if self.charm.lead_control_plane:
            if not self._verify_worker_version(current_version):
                self.set_unit_failed(
                    cause="The k8s worker charm version does not meet the requirements."
                )
                trigger.cancel()
                return

        self.charm.grant_upgrade()

        services = (
            K8S_CONTROL_PLANE_SERVICES + K8S_COMMON_SERVICES
            if self.charm.is_control_plane
            else K8S_COMMON_SERVICES
        )

        try:
            self._perform_upgrade(services=services)
            self.set_unit_completed()

            if self.charm.unit.is_leader():
                self.on_upgrade_changed(event)

            trigger.cancel()
        except SnapError:
            status.add(ops.WaitingStatus("Waiting for the snap to be installed."))
            log.exception("Failed to upgrade the snap. Will retry...")
            trigger.create(reschedule.Period(seconds=30))
            return

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
