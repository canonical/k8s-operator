# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A module for upgrading the k8s and k8s-worker charms."""

import logging
from typing import List, Optional, Union

import charms.contextual_status as status
import ops
import reschedule
from charmlibs.snap import SnapError
from charms.data_platform_libs.v0.upgrade import (
    ClusterNotReadyError,
    DataUpgrade,
    DependencyModel,
    UpgradeFinishedEvent,
    UpgradeGrantedEvent,
    verify_requirements,
)
from inspector import ClusterInspector
from literals import (
    SNAP_NAME,
    UPGRADE_RELATION,
)
from protocols import K8sCharmProtocol

# TODO: (mateoflorido) We are using the compatibility layer for pydantic.v1
#  because the upgrade model does not support pydantic v2 yet.
from pydantic.v1 import BaseModel
from snap import management as snap_management
from snap import version as snap_version

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
        self._upgrade_granted = False
        self._upgrade_complete = False

    @property
    def upgrade_granted(self) -> bool:
        """Check if the upgrade has been granted."""
        return self._upgrade_granted

    def handler(self, event: ops.EventBase):
        """Block reconciler if the unit was upgraded without a pre-upgrade-check.

        Args:
            event: ops.EventBase - event that triggered the check
        """
        if isinstance(event, (UpgradeGrantedEvent, UpgradeFinishedEvent)):
            log.debug("%s, proceed with reconciliation", event.__class__.__name__)
        elif self.state is None:
            log.debug("Upgrade not setup, proceed with reconciliation")
        elif self.state == "upgrading" and (failure := self._upgrade(event)):
            log.warning("Upgrade failure, cease reconciliation: %s", failure.message)
            status.add(failure)
            raise status.ReconcilerError(failure.message)
        elif self.state != "idle":
            failure = self.charm.unit.status.message
            message = f"Upgrade not idle state='{self.state}', cease reconciliation.: {failure}"
            log.warning(message)
            status.add(self.charm.unit.status)
            raise status.ReconcilerError(message)
        elif isinstance(event, ops.UpgradeCharmEvent):
            # A resource attachment can trigger this event
            if self.charm.snap_installation_resource.is_updated:
                log.debug("Resource Attachment, proceed with reconciliation.")
            # or it's a charm upgrade, maybe a single node upgrade that's done?
            elif len(self.app_units) == 1 and self._upgrade_complete:
                log.debug("Single node upgrade completed, proceed with reconciliation.")
            # or maybe a multi-unit upgrade that is underway?
            elif super().upgrade_stack:
                log.debug("Upgrade stack exists, proceed with reconciliation.")
            else:
                reason = "Unit was upgraded without a pre-upgrade-check"
                log.warning(reason)
                self.set_unit_failed(reason)
                if self.charm.unit.is_leader():
                    self.upgrade_stack = self.build_upgrade_stack()
                status.add(self.charm.unit.status)
                raise status.ReconcilerError(reason)

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
        dependency_model: DependencyModel = getattr(self.dependency_model, "k8s_service")
        requirement = dependency_model.dependencies["k8s-worker"]

        incompatible = {
            version: units
            for version, units in worker_versions.items()
            if not verify_requirements(version, requirement)
        }

        if incompatible:
            units_str = "\n".join(
                f"[{v}]: {', '.join(u.name for u in units)}" for v, units in incompatible.items()
            )
            log.error("k8s worker version requirements not met. Incompatible units: %s", units_str)

        return not incompatible

    def _on_upgrade_granted(self, event: UpgradeGrantedEvent) -> None:
        """Handle the upgrade granted event.

        Args:
            event: The UpgradeGrantedEvent instance.
        """
        with status.context(self.charm.unit, exit_status=ops.ActiveStatus("Ready")):
            if failure := self._upgrade(event):
                status.add(failure)
        self._upgrade_complete = True

    def _upgrade(self, event: Union[ops.EventBase, ops.HookEvent]) -> Optional[ops.StatusBase]:
        """Upgrade the snap workload."""
        trigger = reschedule.PeriodicEvent(self.charm)
        current_version, _ = snap_version(SNAP_NAME)

        status.add(ops.MaintenanceStatus("Verifying the cluster is ready for an upgrade."))
        if not current_version:
            message = "Failed to get the version of the k8s snap."
            log.error(message)
            self.set_unit_failed(cause=message)
            return ops.BlockedStatus(message)

        if self.charm.lead_control_plane and not self._verify_worker_versions():
            message = "The k8s worker version requirements are not met."
            log.error(message)
            self.set_unit_failed(cause=message)
            trigger.cancel()
            return ops.BlockedStatus(message)

        self._upgrade_granted = True
        status.add(ops.MaintenanceStatus("Upgrading the snap."))
        try:
            snap_management(self.charm)
            self.set_unit_completed()
            if self.charm.unit.is_leader():
                self.on_upgrade_changed(event)
            trigger.cancel()
        except SnapError:
            log.exception("Failed to upgrade the snap. Will retry...")
            trigger.create(reschedule.Period(seconds=30))
            return ops.WaitingStatus("Waiting for the snap to be installed.")
        self._upgrade_granted = False
        return None

    def build_upgrade_stack(self) -> List[int]:
        """Return a list of unit numbers to upgrade in order.

        Returns:
            A list of unit numbers to upgrade in order.
        """
        units = {self.charm.unit}
        if relation := self.charm.model.get_relation(UPGRADE_RELATION):
            units |= set(relation.units)
        return [int(unit.name.split("/")[-1]) for unit in units]

    def log_rollback_instructions(self) -> None:
        """Log instructions for rolling back the upgrade."""
        log.critical(
            "To rollback the upgrade, run: `juju refresh` to the previously deployed revision."
        )
