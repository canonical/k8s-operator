# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Update status handler for the k8s charm.

This handler is responsible for updating the unit's workload version and status
"""

import logging
from typing import Optional

import charms.contextual_status as status
import ops
import reschedule
from inspector import ClusterInspector
from protocols import K8sCharmProtocol
from snap import version as snap_version
from upgrade import K8sUpgrade

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


class DynamicActiveStatus(ops.ActiveStatus):
    """An ActiveStatus class that can be updated.

    Attributes:
        message (str): explanation of the unit status
        prefix  (str): Optional prefix to the unit status
        postfix (str): Optional postfix to the unit status
    """

    def __init__(self):
        """Initialise the DynamicActiveStatus."""
        super().__init__("Ready")
        self.prefix: str = ""
        self.postfix: str = ""

    @property
    def message(self) -> str:
        """Return the message for the status."""
        pre = f"{self.prefix} :" if self.prefix else ""
        post = f" ({self.postfix})" if self.postfix else ""
        return f"{pre}{self._message}{post}"

    @message.setter
    def message(self, message: str):
        """Set the message for the status.

        Args:
            message (str): explanation of the unit status
        """
        self._message = message


class Handler(ops.Object):
    """Handler for the update-status event in a Kubernetes operator.

    This class observes the `update_status` event and handles it by checking the
    Kubernetes snap status and updating the unit's workload version accordingly.

    Attributes:
        charm (CharmBase): The charm instance that this handler is associated with.
        active_status (DynamicActiveStatus): The active status object used to manage
            the unit's status during the update process.
    """

    def __init__(self, charm: K8sCharmProtocol, upgrade: K8sUpgrade):
        """Initialize the UpdateStatusEvent.

        Args:
            charm: The charm instance that is instantiating this event.
            upgrade: The upgrade instance that handles the upgrade process.
        """
        super().__init__(charm, "update_status")
        self.charm = charm
        self.upgrade = upgrade
        self.active_status = DynamicActiveStatus()
        self.charm.framework.observe(self.charm.on.update_status, self._on_update_status)

    def _on_update_status(self, event: ops.UpdateStatusEvent):
        """Handle update-status event."""
        if not self.charm.reconciler.stored.reconciled:
            return

        try:
            with status.context(self.charm.unit, exit_status=self.active_status):
                self.upgrade.set_upgrade_status(event)
                self.run()
        except status.ReconcilerError:
            log.exception("Can't update_status")

    def kube_system_pods_waiting(self) -> Optional[ops.WaitingStatus]:
        """Check if kube-system pods are waiting.

        Returns:
            WaitingStatus: waiting status if kube-system pods are not ready.
        """
        if self.charm.is_worker:
            # Worker nodes don't need to check the kube-system pods
            return None

        waiting, inspect = None, self.charm.cluster_inspector

        try:
            if failing_pods := inspect.verify_pods_running(["kube-system"]):
                waiting = ops.WaitingStatus(f"Unready Pods: {failing_pods}")
        except ClusterInspector.ClusterInspectorError as e:
            log.exception("Failed to verify pods: %s", e)
            waiting = ops.WaitingStatus("Waiting for API Server")

        return waiting

    def run(self):
        """Check k8s snap status."""
        version, overridden = snap_version("k8s")
        if version:
            self.charm.unit.set_workload_version(version)

        self.active_status.postfix = "Snap Override Active" if overridden else ""

        if not self.charm.get_cluster_name():
            status.add(ops.WaitingStatus("Node not Clustered"))
            return

        trigger = reschedule.PeriodicEvent(self.charm)
        if not self.charm._is_node_ready():
            status.add(ops.WaitingStatus("Node not Ready"))
            trigger.create(reschedule.Period(seconds=30))
            return

        if waiting := self.kube_system_pods_waiting():
            status.add(waiting)
            trigger.create(reschedule.Period(seconds=30))
            return
        trigger.cancel()
