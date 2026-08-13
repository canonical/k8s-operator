#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade Integration tests."""

import logging
import os
import shutil
import subprocess
from typing import Dict, Iterable, Tuple

import jubilant
import pytest
import yaml
from bundle import Bundle, Charm
from cloud import cloud_arch
from helpers import get_leader, wait_pod_phase
from literals import CHARMCRAFT_DIRS

# NOTE: Must satisfy the k8s_service "upgrade_supported" range in
# charms/worker/k8s/src/literals.py -- the charm refuses to upgrade from outside it,
# leaving the control-plane leader blocked. Bump this whenever that range moves.
CHARM_UPGRADE_FROM = os.environ.get("JUJU_DEPLOY_CHANNEL", "1.32/beta")
CONTROL_PLANE_APP = "k8s"
UPGRADE_TIMEOUT = 30 * 60
log = logging.getLogger(__name__)


def charm_channel_missing(charms: Iterable[str], channel: str) -> Tuple[bool, str]:
    """Run to test if a given channel has charms for deployment.

    Args:
        charms: The list of charms to check
        channel: The charm channel to check

    Returns:
        True if the charm channel or any lower risk exists, False otherwise
        Returns a string with the reason if True
    """
    if not shutil.which("juju"):
        # This runs at import time. The CI job that only collects tests (to build the
        # per-module matrix) has no juju CLI, and letting FileNotFoundError escape there
        # turns into a collection error that silently drops this module from the matrix.
        log.warning("juju CLI not found; skipping the upgrade-channel availability check")
        return False, ""

    risk_levels = ["edge", "beta", "candidate", "stable"]
    track, riskiest, *_ = channel.split("/")
    riskiest_level = risk_levels.index(riskiest)
    for app in charms:
        for lookup in risk_levels[riskiest_level:]:
            out = subprocess.check_output(  # noqa: S603
                ["juju", "info", app, "--channel", f"{track}/{lookup}", "--format", "yaml"]
            )
            track_map = yaml.safe_load(out).get("channels", {}).get(track, {})
            if lookup in track_map:
                log.info("Found %s in %s", app, f"{track}/{lookup}")
                break
        else:
            return (
                True,
                f"No suitable channel found for {app} in {channel} to upgrade from",
            )
    return False, ""


not_found, not_found_reason = charm_channel_missing(CHARMCRAFT_DIRS, CHARM_UPGRADE_FROM)

# This pytest mark configures the test environment to use the Canonical Kubernetes
# deploying charms from the edge channels, then upgrading them to the built charm.
pytestmark = [
    pytest.mark.skipif(not_found, reason=not_found_reason),
    pytest.mark.bundle(
        file="test-bundle-dqlite.yaml",
        apps_channel={CONTROL_PLANE_APP: CHARM_UPGRADE_FROM, "k8s-worker": CHARM_UPGRADE_FROM},
    ),
    pytest.mark.architecture("amd64"),
]


def _upgrade_complete(charms: Iterable[str]):
    """Build a jubilant wait predicate that reports whether the upgrade has finished.

    While workers are still upgrading, the control-plane leader is expected to sit in
    "waiting" with a specific message; everything else must be active.

    Args:
        charms: The applications being refreshed.

    Returns:
        A callable suitable for ``jubilant.Juju.wait``'s *ready* argument.
    """
    charms = list(charms)

    def ready(status: jubilant.Status) -> bool:
        k8s_apps = {k: v for k, v in status.apps.items() if k.startswith(CONTROL_PLANE_APP)}
        worker_count = sum(
            len(status.get_units(name)) for name in k8s_apps if name != CONTROL_PLANE_APP
        )
        for name in k8s_apps:
            for unit_name, unit in status.get_units(name).items():
                current = unit.workload_status.current
                message = unit.workload_status.message
                if name == CONTROL_PLANE_APP and unit.leader and worker_count > 0:
                    if current not in ("waiting", "active"):
                        return False
                    if message not in (
                        f"Waiting for {worker_count} Workers to upgrade",
                        "Ready",
                    ):
                        return False
                elif current != "active":
                    return False
        return jubilant.all_agents_idle(status, *charms)

    return ready


def test_upgrade(k8s_cluster: jubilant.Juju, request: pytest.FixtureRequest):
    """Upgrade the model with the provided charms."""
    local_resource: str = request.config.option.snap_installation_resource
    bundle, _ = Bundle.create(request, cloud_arch(k8s_cluster.show_model().controller_name))
    charms: Dict[str, Charm] = bundle.discover_charm_files(request.config.option.charm_files)
    ready = _upgrade_complete(charms)

    k8s_leader = get_leader(k8s_cluster, CONTROL_PLANE_APP)
    wait_pod_phase(k8s_cluster, k8s_leader, None, "Running", namespace="kube-system")

    for app_name, charm in charms.items():
        log.info("Refreshing %s", app_name)
        # juju.run raises TaskError if pre-upgrade-check fails.
        k8s_cluster.run(get_leader(k8s_cluster, app_name), "pre-upgrade-check", wait=300)
        k8s_cluster.refresh(
            app_name,
            path=charm.local_path,
            resources={"snap-installation": local_resource},
        )
        k8s_cluster.wait(
            ready, error=jubilant.any_error, timeout=UPGRADE_TIMEOUT, delay=5, successes=3
        )
        wait_pod_phase(k8s_cluster, k8s_leader, None, "Running", namespace="kube-system")
