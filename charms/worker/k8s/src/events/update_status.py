#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Update status handler for the k8s charm.

This handler is responsible for updating the unit's workload version and status
"""

import logging

import charms.contextual_status as status
import ops
import reschedule
from protocols import K8sCharmProtocol
from snap import version as snap_version

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

    def __init__(self, charm: K8sCharmProtocol):
        """Initialize the UpdateStatusEvent.

        Args:
            charm: The charm instance that is instantiating this event.
        """
        super().__init__(charm, "update_status")
        self.charm = charm
        self.active_status = DynamicActiveStatus()
        self.charm.framework.observe(self.charm.on.update_status, self._on_update_status)

    def _on_update_status(self, _event: ops.UpdateStatusEvent):
        """Handle update-status event."""
        if not self.charm.reconciler.stored.reconciled:
            return

        try:
            with status.context(self.charm.unit, exit_status=self.active_status):
                self.run()
        except status.ReconcilerError:
            log.exception("Can't update_status")

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
        trigger.cancel()
