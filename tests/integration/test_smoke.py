#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging
from pathlib import Path

import juju.model
import pytest
import pytest_asyncio

from .helpers import get_leader, ready_nodes, wait_pod_phase
from .literals import ONE_MIN

log = logging.getLogger(__name__)
LOCAL_APPS = ["k8s"]

pytestmark = [
    pytest.mark.bundle(file="test-smoke.yaml", apps_local=LOCAL_APPS),
]

pinned_revision = (
    "latest/edge" not in Path("charms/worker/k8s/templates/snap_installation.yaml").read_text()
)


@pytest_asyncio.fixture
async def preserve_charm_config(ops_test, kubernetes_cluster: juju.model.Model, timeout: int):
    """Preserve the charm config changes from a test."""
    apps = (kubernetes_cluster.applications[a] for a in LOCAL_APPS)
    pre = await asyncio.gather(*[app.get_config() for app in apps])
    yield pre
    post = await asyncio.gather(*[app.get_config() for app in apps])

    for app_before, app_after in zip(pre, post):
        for key in app_after.keys():
            # Reset any new config keys added by the test to their default
            app_before[key] = str(
                app_after[key]["default"] if key not in app_before else app_before[key]["value"]
            )

    async with ops_test.fast_forward(ONE_MIN):
        await asyncio.gather(*[app.set_config(pre[i]) for i, app in enumerate(apps)])
        await kubernetes_cluster.wait_for_idle(
            apps=LOCAL_APPS, status="active", timeout=timeout * 60
        )


async def test_nodes_ready(kubernetes_cluster: juju.model.Model):
    """Deploy the charm and wait for active/idle status."""
    apps = [kubernetes_cluster.applications[a] for a in LOCAL_APPS]
    units = [u for a in apps for u in a.units]
    expected_nodes = sum(len(a.units) for a in apps)
    await ready_nodes(units[0], expected_nodes)


async def test_kube_system_pods(kubernetes_cluster: juju.model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    await wait_pod_phase(leader, None, "Running", namespace="kube-system")
