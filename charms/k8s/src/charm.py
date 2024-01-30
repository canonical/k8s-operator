#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""K8s Charm.

A machine charm which operates a complete Kubernetes cluster.

This charm installs and operates a Kubernetes cluster via the k8s snap. It exposes
relations to co-operate with other kubernetes components such as optional CNIs,
optional cloud-providers, optional schedulers, external backing stores, and external
certificate storage.
"""

import logging
import re
import shlex
import subprocess
from typing import Optional

import charms.contextual_status as status
import ops
from charms.contextual_status import WaitingStatus, on_error
from charms.k8s.v0.k8sd_api_manager import (
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
    UnixSocketConnectionFactory,
)
from charms.operator_libs_linux.v2.snap import SnapCache, SnapError, SnapState
from charms.reconciler import Reconciler

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]
K8SD_SNAP_SOCKET = "/var/snap/k8s/common/var/lib/k8sd/state/control.socket"


class K8sCharm(ops.CharmBase):
    """A charm for managing a K8s cluster via the k8s snap."""

    def __init__(self, *args):
        """Initialise the K8s charm.

        Args:
            args: Arguments passed to the CharmBase parent constructor.
        """
        super().__init__(*args)

        factory = UnixSocketConnectionFactory(unix_socket=K8SD_SNAP_SOCKET)
        self.api_manager = K8sdAPIManager(factory)
        self.snap_cache = SnapCache()

        self.reconciler = Reconciler(self, self._reconcile)

        self.framework.observe(self.on.update_status, self._on_update_status)

    def _reconcile(self, _):
        """Reconcile state change events."""
        # TODO: Implement clustering using leader units.
        self._install_k8s_snap()
        self._apply_snap_requirements()
        self._bootstrap_k8s_snap()
        self._enable_components()
        self._update_status()

    @on_error(WaitingStatus("Failed to apply snap requirements"), subprocess.CalledProcessError)
    def _apply_snap_requirements(self):
        """Apply necessary snap requirements for the k8s snap.

        This method executes necessary scripts to ensure that the snap
        meets the network and interface requirements.
        """
        status.add(ops.MaintenanceStatus("Applying K8s requirements"))
        commands = [
            "/snap/k8s/current/k8s/connect-interfaces.sh",
            "/snap/k8s/current/k8s/network-requirements.sh",
        ]
        for c in commands:
            subprocess.check_call(shlex.split(c))

    @on_error(ops.WaitingStatus("Failed to bootstrap k8s snap"), subprocess.CalledProcessError)
    def _bootstrap_k8s_snap(self):
        """Bootstrap the k8s if it's not already bootstrapped."""
        if not self.api_manager.is_cluster_bootstrapped():
            status.add(ops.MaintenanceStatus("Bootstrapping Cluster"))
            cmd = "k8s bootstrap"
            subprocess.check_call(shlex.split(cmd))

    @on_error(
        WaitingStatus("Waiting for enable components"), InvalidResponseError, K8sdConnectionError
    )
    def _enable_components(self):
        """Enable necessary components for the Kubernetes cluster."""
        status.add(ops.MaintenanceStatus("Enabling DNS"))
        self.api_manager.enable_component("dns", True)
        status.add(ops.MaintenanceStatus("Enabling Network"))
        self.api_manager.enable_component("network", True)

    def _get_snap_version(self) -> Optional[str]:
        """Retrieve the version of the installed Kubernetes snap package.

        Returns:
            Optional[str]: The version of the installed k8s snap package, or None if
            not available.
        """
        cmd = "snap list k8s"
        result = subprocess.check_output(shlex.split(cmd))
        output = result.decode().strip()
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)

        if match:
            return match.group()

        log.info("Snap k8s not found or no version available.")
        return None

    @on_error(ops.BlockedStatus("Failed to install k8s snap."), SnapError)
    def _install_k8s_snap(self):
        """Install the k8s snap package."""
        status.add(ops.MaintenanceStatus("Installing k8s snap"))
        k8s_snap = self.snap_cache["k8s"]
        if not k8s_snap.present:
            channel = self.config["channel"]
            k8s_snap.ensure(SnapState.Latest, channel=channel)

    @on_error(
        ops.WaitingStatus("Failed to update status"),
        subprocess.CalledProcessError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _update_status(self):
        """Check k8s snap status."""
        if self.api_manager.is_cluster_ready():
            if version := self._get_snap_version():
                self.unit.set_workload_version(version)
        else:
            status.add(ops.WaitingStatus("Waiting for k8s to be ready."))

    def _on_update_status(self, _):
        """Handle update-status event."""
        if not self.reconciler.stored.reconciled:
            return
        with status.context(self.unit):
            self._update_status()


if __name__ == "__main__":  # pragma: nocover
    ops.main.main(K8sCharm)
