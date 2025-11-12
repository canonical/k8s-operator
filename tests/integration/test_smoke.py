#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
from pathlib import Path

import juju.model
import juju.unit
import pytest
from helpers import get_leader, ready_nodes, wait_pod_phase
from literals import ONE_MIN

log = logging.getLogger(__name__)
LOCAL_APPS = ["k8s"]

pytestmark = [
    pytest.mark.bundle(file="test-smoke.yaml", apps_local=LOCAL_APPS),
]

pinned_revision = (
    "latest/edge" not in Path("charms/worker/k8s/templates/snap_installation.yaml").read_text()
)


@pytest.fixture
def preserve_charm_config(jubilant, kubernetes_cluster: juju.model.Model, timeout: int):
    """Preserve the charm config changes from a test."""
    apps = [kubernetes_cluster.applications[a] for a in LOCAL_APPS]
    # MIGRATION: replaced asyncio.gather with list comprehension (jubilant)
    pre = [app.get_config() for app in apps]
    yield pre
    post = [app.get_config() for app in apps]

    for app_before, app_after in zip(pre, post):
        for key in app_after.keys():
            # Reset any new config keys added by the test to their default
            app_before[key] = str(
                app_after[key]["default"] if key not in app_before else app_before[key]["value"]
            )

    # MIGRATION: switched to non-async context manager (jubilant)
    with jubilant.fast_forward(ONE_MIN):
        for i, app in enumerate(apps):
            app.set_config(pre[i])
        kubernetes_cluster.wait_for_idle(apps=LOCAL_APPS, status="active", timeout=timeout * 60)


def test_nodes_ready(kubernetes_cluster: juju.model.Model):
    """Deploy the charm and wait for active/idle status."""
    apps = [kubernetes_cluster.applications[a] for a in LOCAL_APPS]
    units = [u for a in apps for u in a.units]
    expected_nodes = sum(len(a.units) for a in apps)
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    ready_nodes(units[0], expected_nodes)


def test_kube_system_pods(kubernetes_cluster: juju.model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    leader_idx = get_leader(k8s)
    leader = k8s.units[leader_idx]
    wait_pod_phase(leader, None, "Running", namespace="kube-system")
