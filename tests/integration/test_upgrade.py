#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade Integration tests."""

import logging
from typing import Optional

import juju.application
import juju.model
import juju.unit
import pytest
import yaml
from pytest_operator.plugin import OpsTest

from .helpers import Bundle, Charm, get_leader

# This pytest mark configures the test environment to use the Canonical Kubernetes
# deploying charms from the edge channels, then upgrading them to the built charm.
pytestmark = [
    pytest.mark.bundle(
        file="test-bundle.yaml", apps_channel={"k8s": "edge", "k8s-worker": "edge"}
    ),
]


log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_k8s_upgrade(kubernetes_cluster: juju.model.Model, ops_test: OpsTest):
    """Upgrade the model with the provided charms.

    Args:
        kubernetes_cluster: The kubernetes model
        ops_test: The test harness
        request: The request object
    """
    local_resources = {
        "snap-installation": ops_test.request.config.option.snap_installation_resource
    }
    bundle, _ = await Bundle.create(ops_test)
    charms = await bundle.discover_charm_files(ops_test)

    async def _refresh(app_name: str):
        """Refresh the application.

        Args:
            app_name: Name of the application to refresh
        """
        app: Optional[juju.application.Application] = kubernetes_cluster.applications[app_name]
        assert app is not None, f"Application {app_name} not found"

        log.info(f"Refreshing {app_name}")
        leader_idx: int = await get_leader(app)
        leader: juju.unit.Unit = app.units[leader_idx]
        action = await leader.run_action("pre-upgrade-check")
        await action.wait()
        with_fault = f"Pre-upgrade of {app_name} failed with {yaml.safe_dump(action.results)}"
        if app_name == "k8s":
            # The k8s charm has a pre-upgrade-check action that works, k8s-worker does not.
            assert action.status == "completed", with_fault
            assert action.results["return-code"] == 0, with_fault
        await app.refresh(path=charms[app_name].local_path, resources=local_resources)
        await kubernetes_cluster.wait_for_idle(
            apps=list(charms.keys()),
            status="active",
            timeout=30 * 60,
        )

    for app in charms:
        await _refresh(app)
