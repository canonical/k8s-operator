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

import ops
from charms.k8s.v0.k8sd_api_manager import (
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
    UnixSocketConnectionFactory,
)
from charms.operator_libs_linux.v2.snap import SnapCache, SnapError, SnapState
from ops.model import WaitingStatus
from pydantic import ValidationError

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

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
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.install, self._on_install)

        factory = UnixSocketConnectionFactory(unix_socket=K8SD_SNAP_SOCKET)
        self.api_manager = K8sdAPIManager(factory)
        self.snap_cache = SnapCache()

    def _apply_snap_requirements(self):
        """Apply necessary snap requirements for the k8s snap.

        This method executes necessary scripts to ensure that the snap
        meets the network and interface requirements.
        """
        self.unit.status = ops.MaintenanceStatus("Applying K8s requirements")
        commands = [
            "/snap/k8s/current/k8s/connect-interfaces.sh",
            "/snap/k8s/current/k8s/network-requirements.sh",
        ]
        for c in commands:
            subprocess.check_call(shlex.split(c))

    def _bootstrap_k8s_snap(self):
        """Bootstrap the k8s if it's not already bootstrapped."""
        if not self.api_manager.is_cluster_bootstrapped():
            self.unit.status = ops.MaintenanceStatus("Bootstrapping Cluster")
            cmd = "k8s bootstrap"
            subprocess.check_call(shlex.split(cmd), shell=False)

    def _enable_components(self):
        """Enable necessary components for the Kubernetes cluster."""
        self.unit.status = ops.MaintenanceStatus("Enabling DNS")
        self.api_manager.enable_component("dns", True)
        self.unit.status = ops.MaintenanceStatus("Enabling Network")
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

        logger.info("Snap k8s not found or no version available.")
        return None

    def _install_k8s_snap(self):
        """Install the k8s snap package."""
        self.unit.status = ops.MaintenanceStatus("Installing k8s snap")
        k8s_snap = self.snap_cache["k8s"]
        if not k8s_snap.present:
            k8s_snap.ensure(SnapState.Latest, channel="edge")

    def _on_install(self, event):
        """Handle install event for the charm.

        Args:
            event: The event that triggered this handler.
        """
        try:
            self._install_k8s_snap()
            self._apply_snap_requirements()
            self._bootstrap_k8s_snap()
            self._enable_components()
            self.unit.status = WaitingStatus("K8s not ready")
        except (ValidationError, InvalidResponseError) as e:
            logger.warning("Failed to query k8s snap. Reason: %s", e)
            self.unit.status = ops.WaitingStatus("Waiting for K8sd API.")
        except SnapError as e:
            logger.error("Failed to install k8s snap. Reason: %s", e.message)
            self.unit.status = ops.BlockedStatus("Failed to install k8s snap")
        except subprocess.CalledProcessError as e:
            logger.error("Failed to run subprocess: %s", e)
        except K8sdConnectionError as e:
            logger.warning("Unable to contact K8sd API: %s", e)
            self.unit.status = ops.WaitingStatus("Waiting for K8sd API.")
        finally:
            event.defer()

    def _on_update_status(self, event: ops.UpdateStatusEvent):
        """Handle update-status event.

        Args:
            event: event triggering the handler.
        """
        try:
            if self.api_manager.is_cluster_ready():
                if version := self._get_snap_version():
                    self.unit.set_workload_version(version)
                self.unit.status = ops.ActiveStatus("Ready")
        except ValidationError:
            self.unit.status = ops.WaitingStatus("Waiting for K8s to be ready")
        except K8sdConnectionError:
            logger.exception("Unable to contact K8sdAPI")
            self.unit.status = ops.WaitingStatus("Waiting for K8sd API")
        finally:
            event.defer()


if __name__ == "__main__":  # pragma: nocover
    ops.main.main(K8sCharm)
