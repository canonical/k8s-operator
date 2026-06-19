#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
from pathlib import Path

import jubilant
import pytest
from helpers import fast_forward, get_leader, ready_nodes, wait_pod_phase
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
def preserve_charm_config(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Preserve the charm config changes from a test."""
    pre = {app: kubernetes_cluster.config(app) for app in LOCAL_APPS}
    yield pre

    with fast_forward(kubernetes_cluster, ONE_MIN):
        for app, before in pre.items():
            current = kubernetes_cluster.config(app)
            # Reset any new config keys added by the test to their default
            to_reset = [key for key in current if key not in before]
            if to_reset:
                kubernetes_cluster.config(app, reset=to_reset)
            if before:
                kubernetes_cluster.config(app, before)
        kubernetes_cluster.wait(
            lambda status: jubilant.all_active(status, *LOCAL_APPS), timeout=timeout * 60
        )


def test_nodes_ready(kubernetes_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    units = [unit for app in LOCAL_APPS for unit in status.get_units(app)]
    expected_nodes = sum(len(status.get_units(app)) for app in LOCAL_APPS)
    ready_nodes(kubernetes_cluster, units[0], expected_nodes)


def test_kube_system_pods(kubernetes_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    leader = get_leader(kubernetes_cluster, "k8s")
    wait_pod_phase(kubernetes_cluster, leader, None, "Running", namespace="kube-system")
