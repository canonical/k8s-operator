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
import socket
import subprocess
from pathlib import Path
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
KUBECONFIG = Path.home() / ".kube/config"
ETC_KUBERNETES = Path("/etc/kubernetes")
K8SD_PORT = 6400


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

        factory = UnixSocketConnectionFactory(unix_socket=K8SD_SNAP_SOCKET, timeout=320)
        self.api_manager = K8sdAPIManager(factory)
        self.snap_cache = SnapCache()

        self.reconciler = Reconciler(self, self._reconcile)

        self.is_worker = self.meta.name == "k8s-worker"
        self.framework.observe(self.on.update_status, self._on_update_status)

    @property
    def is_control_plane(self) -> bool:
        """Returns true if the unit is not a worker."""
        return not self.is_worker

    def _reconcile(self, _):
        """Reconcile state change events."""
        self._install_k8s_snap()
        self._apply_snap_requirements()
        self._check_k8sd_ready()
        if self.unit.is_leader() and self.is_control_plane:
            self._bootstrap_k8s_snap()
            self._enable_components()
            self._create_cluster_tokens()
        self._join_cluster()
        self._update_status()
        if self.is_control_plane:
            self._generate_kubeconfig()

    def _get_node_name(self) -> str:
        """Return the lowercase hostname.

        Returns:
            the hostname of the machine.
        """
        return socket.gethostname().lower()

    @on_error(ops.BlockedStatus("Failed to install k8s snap."), SnapError)
    def _install_k8s_snap(self):
        """Install the k8s snap package."""
        status.add(ops.MaintenanceStatus("Installing k8s snap"))
        k8s_snap = self.snap_cache["k8s"]
        if not k8s_snap.present:
            channel = self.config["channel"]
            k8s_snap.ensure(SnapState.Latest, channel=channel)

    @on_error(WaitingStatus("Failed to apply snap requirements"), subprocess.CalledProcessError)
    def _apply_snap_requirements(self):
        """Apply necessary snap requirements for the k8s snap.

        This method executes necessary scripts to ensure that the snap
        meets the network and interface requirements.
        """
        status.add(ops.MaintenanceStatus("Applying K8s requirements"))
        init_sh = "/snap/k8s/current/k8s/hack/init.sh"
        subprocess.check_call(shlex.split(init_sh))

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
        if not self.api_manager.is_cluster_bootstrapped():
            status.add(ops.MaintenanceStatus("Bootstrapping Cluster"))
            binding = self.model.get_binding("juju-info")
            address = binding and binding.network.ingress_address
            node_name = self._get_node_name()
            # TODO: Make port (and address) configurable.
            self.api_manager.bootstrap_k8s_snap(node_name, f"{str(address)}:{K8SD_PORT}")

    def _distribute_cluster_tokens(self, relation: ops.Relation, token_type: str):
        """Distribute role based tokens as secrets on a relation.

        Args:
            relation (ops.Relation): The relation for which to create tokens
            token_type (str): Either "control-plane" or "worker"
        """
        units = {u for u in relation.units if u.name != self.unit.name}
        app_databag: ops.RelationDataContent | dict[str, str] = relation.data.get(
            self.model.app, {}
        )

        for unit in units:
            sec_key = f"{unit.name}-cluster-secret"
            if app_databag.get(sec_key):
                continue
            if not (name := relation.data[unit].get("node-name")):
                # wait for the joining unit to provide its node-name
                continue

            token = self.api_manager.create_join_token(
                name, worker=token_type == "worker"  # nosec
            )
            content = {"token": token}
            secret = self.app.add_secret(content)
            secret.grant(relation, unit=unit)
            relation.data[self.app][sec_key] = secret.id or ""

    @on_error(
        WaitingStatus("Waiting for enable components"), InvalidResponseError, K8sdConnectionError
    )
    def _enable_components(self):
        """Enable necessary components for the Kubernetes cluster."""
        status.add(ops.MaintenanceStatus("Enabling DNS"))
        self.api_manager.enable_component("dns", True)
        status.add(ops.MaintenanceStatus("Enabling Network"))
        self.api_manager.enable_component("network", True)

    def _create_cluster_tokens(self):
        """Create tokens for the units in the cluster and k8s-cluster relations."""
        if not self.unit.is_leader() or not self.is_control_plane:
            return

        if peer := self.model.get_relation("cluster"):
            self._distribute_cluster_tokens(peer, token_type="control-plane")  # nosec

        if workers := self.model.get_relation("k8s-cluster"):
            self._distribute_cluster_tokens(workers, token_type="worker")  # nosec

    @on_error(
        WaitingStatus("Waiting for Cluster token"),
        AssertionError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _join_cluster(self):
        """Retrieve the join token from secret databag and join the cluster."""
        if self.api_manager.is_cluster_bootstrapped():
            return

        status.add(ops.MaintenanceStatus("Joining cluster"))

        def _req_cluster_token(relation: ops.Relation):
            """Provide a requested node-name.

            Args:
                relation: juju relation on which to operate
            """
            relation.data[self.unit]["node-name"] = self._get_node_name()

        def _rec_cluster_token(relation: ops.Relation) -> str:
            """Recover token from cluster-secret.

            Args:
                relation: juju relation on which to operate

            Returns:
                str: The token recovered from the juju provided by k8s leader
            """
            sec_databags, sec_key = [], f"{self.unit.name}-cluster-secret"
            for potential in relation.data.values():
                if sec_key in potential:
                    sec_databags.append(potential)
                    break
            assert len(sec_databags) == 1, "Failed to find 1 cluster-secret"  # nosec

            secret_id = sec_databags[0][sec_key]
            assert secret_id, "cluster:secret-id is not set"  # nosec
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content(refresh=True)
            assert content["token"], "cluster: token not valid"  # nosec
            return content["token"]

        if relation := self.model.get_relation("cluster"):
            _req_cluster_token(relation)
            token = _rec_cluster_token(relation)
            binding = self.model.get_binding("juju-info")
            address = binding and binding.network.ingress_address
            name = self._get_node_name()
            self.api_manager.join_cluster(name, f"{str(address)}:{K8SD_PORT}", token)

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

    @on_error(
        ops.WaitingStatus("Cluster not yet ready"),
        AssertionError,
        subprocess.CalledProcessError,
        InvalidResponseError,
        K8sdConnectionError,
    )
    def _update_status(self):
        """Check k8s snap status."""
        if self.is_worker:
            relation = self.model.get_relation("cluster")
            assert relation, "Missing cluster relation with k8s"  # nosec
        else:
            assert self.api_manager.is_cluster_ready(), "control-plane not yet ready"  # nosec
        if version := self._get_snap_version():
            self.unit.set_workload_version(version)

    @on_error(ops.WaitingStatus(""))
    def _generate_kubeconfig(self):
        """Generate kubeconfig."""
        status.add(ops.MaintenanceStatus("Generating KubeConfig"))
        KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
        src = ETC_KUBERNETES / ("admin.conf" if self.is_control_plane else "kubelet.conf")
        KUBECONFIG.write_bytes(src.read_bytes())

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
