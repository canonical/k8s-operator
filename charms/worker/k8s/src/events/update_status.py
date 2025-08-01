# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Update status handler for the k8s charm.

This handler is responsible for updating the unit's workload version and status
"""

import logging
from typing import List, Optional, cast

import ops
import reschedule
from config.bootstrap import detect_bootstrap_config_changes
from inspector import ClusterInspector
from k8s.node import Status, ready
from protocols import K8sCharmProtocol
from snap import version as snap_version
from upgrade import K8sUpgrade

import charms.contextual_status as status
import charms.k8s.v0.k8sd_api_manager as api_manager

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


class DynamicActiveStatus(ops.ActiveStatus):
    """An ActiveStatus class that can be updated.

    Attributes:
        message (str): explanation of the unit status
        prefix  (str): Optional prefix to the unit status
        postfix (str): Optional postfix to the unit status
    """

    def __init__(self, msg="Ready") -> None:
        """Initialise the DynamicActiveStatus."""
        super().__init__(msg)
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
                self.upgrade.handler(event)
                self.run()
        except status.ReconcilerError:
            log.exception("Can't update_status")

    def failed_features(self) -> Optional[ops.StatusBase]:
        """Check if any features have failed.

        Returns:
            BlockedStatus: blocked status if any features have failed.
            WaitingStatus: waiting status if the API Manager is unavailable.
        """
        if self.charm.is_worker:
            # Worker nodes don't need to check the failed features
            log.debug("Skipping feature verification for worker node")
            return None

        try:
            cluster_status = self.charm.api_manager.get_cluster_status()
        except api_manager.K8sdAPIManagerError as e:
            log.exception("Failed to verify features: %s", e)
            return ops.WaitingStatus("Waiting to verify features")

        feature_status: List[ops.StatusBase] = []
        if meta := cluster_status.metadata:
            for name, f_config, f_status in meta.status.by_feature:
                if not f_config:
                    log.warning("Feature '%s' has no config", name)
                elif not f_status:
                    log.warning("Feature '%s' has no status", name)
                else:
                    to_log = log.info
                    if f_config.enabled and not f_status.enabled:
                        to_log = log.error
                        feature_status.append(ops.BlockedStatus(f"Feature '{name}' is not ready"))
                    to_log(
                        "Feature '%s' enabled=%s,deployed=%s,ver=%s,updated_at=%s: %s",
                        name,
                        f_config.enabled,
                        f_status.enabled,
                        f_status.version,
                        f_status.updated_at,
                        f_status.message,
                    )
        else:
            log.warning("Cluster status is missing feature statuses")

        return next(iter(feature_status), None)

    def unready_pods_waiting(self) -> Optional[ops.WaitingStatus]:
        """Check if any pods are not ready.

        Returns:
            WaitingStatus: waiting status if pods are not ready.
        """
        if self.charm.is_worker:
            # Worker nodes don't need to check the unready pods
            return None

        waiting, inspect = None, self.charm.cluster_inspector
        namespace_cfg = cast(str, self.charm.config["unready-pod-namespaces"])
        namespaces = namespace_cfg.split()

        try:
            if failing_pods := inspect.verify_pods_running(namespaces):
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

        name = self.charm.get_node_name()
        trigger = reschedule.PeriodicEvent(self.charm)
        readiness = ready(self.charm.kubeconfig, name)
        final_status = None

        if final_status := detect_bootstrap_config_changes(self.charm):
            log.error("Bootstrap config changes detected: %s", final_status.message)
        elif final_status := self.failed_features():
            log.error("Failed features reported: %s", final_status.message)
        elif readiness != Status.READY:
            log.warning("Node %s is %s", name, readiness.value)
            final_status = ops.WaitingStatus(f"Node {name} {readiness.value}")
        elif final_status := self.unready_pods_waiting():
            log.warning("Unready pods detected: %s", final_status.message)
        if final_status:
            status.add(final_status)
            trigger.create(reschedule.Period(seconds=30))
        else:
            trigger.cancel()
