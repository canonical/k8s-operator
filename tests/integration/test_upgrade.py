#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade Integration tests."""

import datetime
import logging
import os
import subprocess
from typing import Iterable, Optional, Tuple

import juju.application
import juju.model
import juju.unit
import pytest
import yaml
from helpers import CHARMCRAFT_DIRS, Bundle, get_leader, wait_pod_phase
from pytest_operator.plugin import OpsTest
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
        file="test-bundle-dqlite.yaml",
        apps_channel={CONTROL_PLANE_APP: CHARM_UPGRADE_FROM, "k8s-worker": CHARM_UPGRADE_FROM},
    ),
    pytest.mark.architecture("amd64"),
]


async def test_upgrade(kubernetes_cluster: juju.model.Model, ops_test: OpsTest):
    """Upgrade the model with the provided charms.

    Args:
        kubernetes_cluster: The kubernetes model
        ops_test: The test harness
        request: The request object
    """

    @retry(
        wait=wait_fixed(5),
        stop=stop_after_delay(datetime.timedelta(minutes=30)),
        before_sleep=before_sleep_log(log, logging.WARNING),
        retry=retry_if_not_exception_type(juju.model.JujuUnitError),
    )
    async def _wait_for_upgrade_complete() -> None:
        """Wait for the model to become idle."""
        k8s_apps = {
            k: v
            for k, v in kubernetes_cluster.applications.items()
            if k.startswith(CONTROL_PLANE_APP)
        }
        worker_apps = {k: v for k, v in k8s_apps.items() if k != CONTROL_PLANE_APP}
        worker_count = sum(len(w.units) for w in worker_apps.values())
        await kubernetes_cluster.wait_for_idle(apps=list(charms.keys()), timeout=30)

        # Check workload status individually, as the k8s leader may be in a different state
        leader_idx: int = await get_leader(k8s)
        for name, app in k8s_apps.items():
            for idx, unit in enumerate(app.units):
                err = f"{unit.name} has not completed upgrade: {unit.workload_status_message}"
                status, message = unit.workload_status, unit.workload_status_message
                if status == "error":
                    raise juju.model.JujuUnitError(message)
                if name == CONTROL_PLANE_APP and idx == leader_idx and worker_count > 0:
                    assert status in ["waiting", "active"], err
                    assert message in [
                        f"Waiting for {worker_count} Workers to upgrade",
                        "Ready",
                    ], err
                else:
                    assert status == "active", err

    async def _refresh(model: juju.model.Model, app_name: str):
        """Refresh the application.

        Args:
            model: The model to refresh the application in
            app_name: Name of the application to refresh
        """
        app: Optional[juju.application.Application] = model.applications[app_name]
        assert app is not None, f"Application {app_name} not found"

        log.info("Refreshing %s", app_name)
        leader_idx: int = await get_leader(app)
        leader: juju.unit.Unit = app.units[leader_idx]
        action = await leader.run_action("pre-upgrade-check")
        resources = {"snap-installation": local_resource}
        await action.wait()
        with_fault = f"Pre-upgrade of '{app_name}' failed with {yaml.safe_dump(action.results)}"
        assert action.status == "completed", with_fault
        assert action.results["return-code"] == 0, with_fault
        await app.refresh(path=charms[app_name].local_path, resources=resources)
        await _wait_for_upgrade_complete()

    k8s = kubernetes_cluster.applications["k8s"]
    k8s_leader_idx: int = await get_leader(k8s)
    k8s_leader: juju.unit.Unit = k8s.units[k8s_leader_idx]

    await wait_pod_phase(k8s_leader, None, "Running", namespace="kube-system")

    local_resource: str = ops_test.request.config.option.snap_installation_resource
    bundle, _ = await Bundle.create(ops_test)
    charms = await bundle.discover_charm_files(ops_test)
    for app in charms:
        await _refresh(kubernetes_cluster, app)
        await wait_pod_phase(k8s_leader, None, "Running", namespace="kube-system")
