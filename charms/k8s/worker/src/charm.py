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
    """A charm for managing a K8s cluster via the k8s snap.

    Attrs:
        is_worker: true if this is a worker charm unit
        is_control_plane: true if this is a control-plane charm unit
    """

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

        self.is_worker = self.meta.name == "k8s-worker"
        self.framework.observe(self.on.update_status, self._on_update_status)

    @property
    def is_control_plane(self) -> bool:
        """Returns true if the unit is not a worker."""
        return not self.is_worker

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

    @on_error(WaitingStatus("Waiting for k8sd"), InvalidResponseError, K8sdConnectionError)
    def _check_k8sd_ready(self):
        """Check if k8sd is ready to accept requests."""
        status.add(ops.MaintenanceStatus("Check k8sd ready"))
        self.api_manager.check_k8sd_ready()

    @on_error(
        ops.WaitingStatus("Failed to bootstrap k8s snap"),
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _bootstrap_k8s_snap(self):
        """Bootstrap k8s if it's not already bootstrapped."""
        # TODO: Remove `is_cluster_bootstrapped` check once
        # https://github.com/canonical/k8s-snap/pull/99 landed.
        if not self.api_manager.is_cluster_bootstrapped():
            status.add(ops.MaintenanceStatus("Bootstrapping Cluster"))
            binding = self.model.get_binding("juju-info")
            address = binding and binding.network.ingress_address
            # k8s/x to k8s-x to avoid trouble with urls
            name = self.unit.name.replace("/", "-")

            # TODO: Make port (and address) configurable.
            self.api_manager.bootstrap_k8s_snap(name, f"{str(address)}:6400")

    def _distribute_cluster_tokens(self, relation, _role):
        """Distribute role based tokens as secrets on a relation.

        Args:
            relation: The relation for which to create tokens
            _role: "worker" or "control-plane" role
        """
        units = {u for u in relation.units if u.name != self.unit.name}
        app_databag = relation.data.get(self.model.app, {})

        for unit in units:
            if app_databag.get(unit.name):
                continue

            name = unit.name.replace("/", "-")
            token = self.api_manager.create_join_token(name)
            content = {"token": token}
            secret = self.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.app][unit.name] = secret.id or ""

    def _create_cluster_tokens(self):
        """Create tokens for the units in the cluster and k8s-cluster relations."""
        if not self.unit.is_leader() or not self.is_control_plane:
            return

        if peer := self.model.get_relation("cluster"):
            self._distribute_cluster_tokens(peer, "control-plane")

        # TODO handle requesting cluster tokens for workers

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

    @on_error(WaitingStatus("Waiting for Cluster token"), TypeError)
    def _join_cluster(self):
        """Retrieve the join token from secret databag and join the cluster."""
        if self.api_manager.is_cluster_bootstrapped():
            return

        status.add(ops.MaintenanceStatus("Joining cluster"))

        if relation := self.model.get_relation("cluster"):
            app_databag = relation.data.get(self.model.app, {})
            secret_id = app_databag.get(self.unit.name, "")
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content()
            token = content["token"]
            cmd = f"k8s join-cluster {shlex.quote(token)}"
            subprocess.check_call(shlex.split(cmd))

    def _reconcile(self, _):
        """Reconcile state change events."""
        self._install_k8s_snap()
        self._apply_snap_requirements()
        if self.unit.is_leader() and self.is_control_plane:
            self._bootstrap_k8s_snap()
            self._enable_components()
            self._create_cluster_tokens()
        self._join_cluster()
        self._update_status()

    @on_error(
        ops.WaitingStatus("Cluster not yet ready"),
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

    def _on_update_status(self, _event: ops.UpdateStatusEvent):
        """Handle update-status event."""
        if not self.reconciler.stored.reconciled:
            return
        try:
            with status.context(self.unit):
                self._update_status()
        except status.ReconcilerError:
            log.exception("Can't to update_status")


if __name__ == "__main__":  # pragma: nocover
    ops.main.main(K8sCharm)
