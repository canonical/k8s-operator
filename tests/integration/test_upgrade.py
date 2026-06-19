#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade Integration tests."""

import datetime
import logging
import os
import subprocess
from typing import Iterable, Tuple

import jubilant
import pytest
import yaml
from helpers import CHARMCRAFT_DIRS, Bundle, get_leader, wait_pod_phase
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_not_exception_type,
    stop_after_delay,
    wait_fixed,
)

CHARM_UPGRADE_FROM = os.environ.get("JUJU_DEPLOY_CHANNEL", "1.32/beta")
CONTROL_PLANE_APP = "k8s"
log = logging.getLogger(__name__)


class UpgradeError(Exception):
    """Raised when a unit reaches an error state during an upgrade."""


def charm_channel_missing(charms: Iterable[str], channel: str) -> Tuple[bool, str]:
    """Run to test if a given channel has charms for deployment.

    Args:
        charms: The list of charms to check
        channel: The charm channel to check

    Returns:
        True if the charm channel or any lower risk exists, False otherwise
        Returns a string with the reason if True
    """
    risk_levels = ["edge", "beta", "candidate", "stable"]
    track, riskiest, *_ = channel.split("/")
    riskiest_level = risk_levels.index(riskiest)
    for app in charms:
        for lookup in risk_levels[riskiest_level:]:
            out = subprocess.check_output(
                [
                    "juju",
                    "info",
                    app,
                    "--channel",
                    f"{track}/{lookup}",
                    "--format",
                    "yaml",
                ]
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
        file="test-bundle.yaml",
        apps_channel={CONTROL_PLANE_APP: CHARM_UPGRADE_FROM, "k8s-worker": CHARM_UPGRADE_FROM},
    ),
    pytest.mark.architecture("amd64"),
]


def test_upgrade(kubernetes_cluster: jubilant.Juju, request):
    """Upgrade the model with the provided charms.

    Args:
        kubernetes_cluster: The kubernetes model juju instance
        request: The pytest request object
    """

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_delay(datetime.timedelta(minutes=30)),
        before_sleep=before_sleep_log(log, logging.WARNING),
        retry=retry_if_not_exception_type(UpgradeError),
    )
    def _wait_for_upgrade_complete() -> None:
        """Wait for the model to become idle."""
        status = kubernetes_cluster.status()
        k8s_apps = [name for name in status.apps if name.startswith(CONTROL_PLANE_APP)]
        worker_apps = [name for name in k8s_apps if name != CONTROL_PLANE_APP]
        worker_count = sum(len(status.get_units(name)) for name in worker_apps)
        kubernetes_cluster.wait(lambda s: jubilant.all_agents_idle(s, *charms), timeout=30)

        # Check workload status individually, as the k8s leader may be in a different state
        leader = get_leader(kubernetes_cluster, CONTROL_PLANE_APP)
        status = kubernetes_cluster.status()
        for name in k8s_apps:
            for unit_name, unit in status.get_units(name).items():
                ws = unit.workload_status
                err = f"{unit_name} has not completed upgrade: {ws.message}"
                if ws.current == "error":
                    raise UpgradeError(ws.message)
                if name == CONTROL_PLANE_APP and unit_name == leader and worker_count > 0:
                    assert ws.current in ["waiting", "active"], err
                    assert ws.message in [
                        f"Waiting for {worker_count} Workers to upgrade",
                        "Ready",
                    ], err
                else:
                    assert ws.current == "active", err

    def _refresh(app_name: str):
        """Refresh the application.

        Args:
            app_name: Name of the application to refresh
        """
        log.info("Refreshing %s", app_name)
        leader = get_leader(kubernetes_cluster, app_name)
        kubernetes_cluster.run(leader, "pre-upgrade-check")
        resources = {"snap-installation": local_resource}
        kubernetes_cluster.refresh(app_name, path=charms[app_name].local_path, resources=resources)
        _wait_for_upgrade_complete()

    k8s_leader = get_leader(kubernetes_cluster, "k8s")

    wait_pod_phase(kubernetes_cluster, k8s_leader, None, "Running", namespace="kube-system")

    local_resource: str = request.config.option.snap_installation_resource
    bundle, _ = Bundle.create(request, kubernetes_cluster)
    charms = bundle.discover_charm_files(request.config.option.charm_files or [])
    for app in charms:
        _refresh(app)
        wait_pod_phase(kubernetes_cluster, k8s_leader, None, "Running", namespace="kube-system")
