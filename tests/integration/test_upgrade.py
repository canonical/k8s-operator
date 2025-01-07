#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade Integration tests."""

import logging
import os
import subprocess
from typing import Iterable, Optional, Tuple

import juju.application
import juju.model
import juju.unit
import pytest
import yaml
from pytest_operator.plugin import OpsTest

from .helpers import CHARMCRAFT_DIRS, Bundle, get_leader, wait_pod_phase

CHARM_UPGRADE_FROM = os.environ.get("JUJU_DEPLOY_CHANNEL", "1.32/beta")
log = logging.getLogger(__name__)


def charm_channel_missing(charms: Iterable[str], channel: str) -> Tuple[bool, str]:
    """Run to test if a given channel has charms for deployment

    Args:
        charms: The list of charms to check
        channel: The charm channel to check

    Returns:
        True if the charm channel or any lower risk exists, False otherwise
        Returns a string with the reason if True
    """
    risk_levels = ["edge", "beta", "candidate", "stable"]
    track, riskiest = channel.split("/")
    riskiest_level = risk_levels.index(riskiest)
    for app in charms:
        for lookup in risk_levels[riskiest_level:]:
            out = subprocess.check_output(
                ["juju", "info", app, "--channel", f"{track}/{lookup}", "--format", "yaml"]
            )
            track_map = yaml.safe_load(out).get("channels", {}).get(track, {})
            if lookup in track_map:
                log.info("Found %s in %s", app, f"{track}/{lookup}")
                break
        else:
            return True, f"No suitable channel found for {app} in {channel} to upgrade from"
    return False, ""


not_found, not_found_reason = charm_channel_missing(CHARMCRAFT_DIRS, CHARM_UPGRADE_FROM)

# This pytest mark configures the test environment to use the Canonical Kubernetes
# deploying charms from the edge channels, then upgrading them to the built charm.
pytestmark = [
    pytest.mark.skipif(not_found, reason=not_found_reason),
    pytest.mark.bundle(
        file="test-bundle.yaml",
        apps_channel={"k8s": CHARM_UPGRADE_FROM, "k8s-worker": CHARM_UPGRADE_FROM},
    ),
]


async def test_upgrade(kubernetes_cluster: juju.model.Model, ops_test: OpsTest):
    """Upgrade the model with the provided charms.

    Args:
        kubernetes_cluster: The kubernetes model
        ops_test: The test harness
        request: The request object
    """

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
        resources = {"snap-installation": local_resources}
        await action.wait()
        with_fault = f"Pre-upgrade of '{app_name}' failed with {yaml.safe_dump(action.results)}"
        assert action.status == "completed", with_fault
        assert action.results["return-code"] == 0, with_fault
        await app.refresh(path=charms[app_name].local_path, resources=resources)
        await model.wait_for_idle(
            apps=list(charms.keys()),
            status="active",
            timeout=30 * 60,
        )

    k8s = kubernetes_cluster.applications["k8s"]
    k8s_leader_idx: int = await get_leader(k8s)
    k8s_leader: juju.unit.Unit = k8s.units[k8s_leader_idx]

    await wait_pod_phase(k8s_leader, None, "Running", namespace="kube-system")

    local_resources = ops_test.request.config.option.snap_installation_resource
    bundle, _ = await Bundle.create(ops_test)
    charms = await bundle.discover_charm_files(ops_test)
    for app in charms:
        await _refresh(kubernetes_cluster, app)
        await wait_pod_phase(k8s_leader, None, "Running", namespace="kube-system")
