# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""A module for upgrading the k8s and k8s-worker charms."""

import logging
from typing import List, Union

import ops
import reschedule
from inspector import ClusterInspector
from literals import (
    BOOTSTRAP_DATASTORE,
    K8S_CONTROL_PLANE_SERVICES,
    K8S_DQLITE_SERVICE,
    MANAGED_ETCD_SERVICE,
    K8S_WORKER_SERVICES,
    SNAP_NAME,
    UPGRADE_RELATION,
)
from protocols import K8sCharmProtocol

# TODO: (mateoflorido) We are using the compatibility layer for pydantic.v1
#  because the upgrade model does not support pydantic v2 yet.
from pydantic.v1 import BaseModel
from snap import management as snap_management
from snap import start, stop
from snap import version as snap_version

import charms.contextual_status as status
from charms.data_platform_libs.v0.upgrade import (
    ClusterNotReadyError,
    DataUpgrade,
    DependencyModel,
    UpgradeGrantedEvent,
    verify_requirements,
)
from charms.operator_libs_linux.v2.snap import SnapError

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

    def __init__(self, charm: K8sCharmProtocol, cluster_inspector: ClusterInspector, **kwargs):
        """Initialize the K8sUpgrade.

        Args:
            charm: The charm instance.
            cluster_inspector: The ClusterInspector instance.
            kwargs: Additional keyword arguments.
        """
        super().__init__(charm, **kwargs)
        self.charm = charm
        self.cluster_inspector = cluster_inspector

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
        if self.charm.is_worker:
            log.info("TODO: Find some pre-upgrade checks for worker application.")
            return
        try:
            nodes = self.cluster_inspector.get_nodes()
            failing_pods = self.cluster_inspector.verify_pods_running(["kube-system"])
        except ClusterInspector.ClusterInspectorError as e:
            raise ClusterNotReadyError(
                message="Cluster is not ready for an upgrade",
                cause=str(e),
                resolution="""API server may not be reachable.
                Please check that the API server is up and running.""",
            ) from e

        unready_nodes = nodes or []

        if unready_nodes:
            joined = ", ".join(
                [
                    node.metadata.name
                    for node in unready_nodes
                    if node.metadata and node.metadata.name
                ]
            )
            raise ClusterNotReadyError(
                message="Cluster is not ready for an upgrade",
                cause=f"Nodes not ready: {joined}",
                resolution="""Node(s) may be in a bad state.
                    Please check the node(s) for more information.""",
            )

        if failing_pods:
            raise ClusterNotReadyError(
                message="Cluster is not ready",
                cause=f"Pods not running in namespace(s): {failing_pods}",
                resolution="Check the logs for the failing pods.",
            )

    def _verify_worker_versions(self) -> bool:
        """Verify that the k8s-worker charm versions meet the requirements.

        This method verifies that all applications related to the cluster relation
        satisfy the requirements of the k8s-worker charm.

        Returns:
            bool: True if all worker versions meet the requirements, False otherwise.
        """
        worker_versions = self.charm.get_worker_versions()
        if not worker_versions:
            return True
        dependency_model: DependencyModel = getattr(self.dependency_model, "k8s_service")

        incompatible = {
            version: units
            for version, units in worker_versions.items()
            if not verify_requirements(
                version=version, requirement=dependency_model.dependencies["k8s-worker"]
            )
        }

        if incompatible:
            units_str = "\n".join(
                f"[{v}]: {', '.join(u.name for u in units)}" for v, units in incompatible.items()
            )
            log.error(
                "k8s worker charm version requirements not met. Incompatible units: %s", units_str
            )
            return False

        return True

    def _perform_upgrade(self, services: List[str]) -> None:
        """Perform the upgrade.

        Args:
            services: The services to stop and start during the upgrade.
        """
        status.add(ops.MaintenanceStatus("Stopping the K8s services"))
        stop(SNAP_NAME, services)
        status.add(ops.MaintenanceStatus("Upgrading the k8s snap."))
        snap_management(self.charm)
        status.add(ops.MaintenanceStatus("Starting the K8s services"))
        start(SNAP_NAME, services)

    def _on_upgrade_granted(self, event: UpgradeGrantedEvent) -> None:
        """Handle the upgrade granted event.

        Args:
            event: The UpgradeGrantedEvent instance.
        """
        with status.context(self.charm.unit, exit_status=ops.ActiveStatus("Ready")):
            self._upgrade(event)

    def _upgrade(self, event: Union[ops.EventBase, ops.HookEvent]) -> None:
        """Upgrade the snap workload."""
        trigger = reschedule.PeriodicEvent(self.charm)
        current_version, _ = snap_version("k8s")

        status.add(ops.MaintenanceStatus("Verifying the cluster is ready for an upgrade."))
        if not current_version:
            log.error("Failed to get the version of the k8s snap.")
            self.set_unit_failed(cause="Failed to get the version of the k8s snap.")
            status.add(ops.BlockedStatus("Failed to get the version of the k8s snap."))
            return

        status.add(ops.MaintenanceStatus("Upgrading the charm."))

        if self.charm.lead_control_plane:
            if not self._verify_worker_versions():
                self.set_unit_failed(
                    cause="The k8s worker charm version does not meet the requirements."
                )
                trigger.cancel()
                return

        self.charm.grant_upgrade()
        if self.charm.is_control_plane:
            services = list(K8S_CONTROL_PLANE_SERVICES)
            bootstrap_datastore = BOOTSTRAP_DATASTORE.get(self.charm)
            if bootstrap_datastore != "dqlite":
                services.remove(K8S_DQLITE_SERVICE)
            if bootstrap_datastore != "managed-etcd":
                services.remove(MANAGED_ETCD_SERVICE)
        else:
            services = list(K8S_WORKER_SERVICES)

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
        relation = self.charm.model.get_relation(UPGRADE_RELATION)
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
