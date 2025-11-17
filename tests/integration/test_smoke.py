#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
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
    apps = [a for a in kubernetes_cluster.status().apps if a in LOCAL_APPS]
    pre = [dict(kubernetes_cluster.config(app)) for app in apps]
    yield pre
    post = [kubernetes_cluster.config(app) for app in apps]

    for app_before, app_after in zip(pre, post):
        for key in app_after.keys():
            # Reset any new config keys added by the test to their default
            app_before[key] = str(app_after[key] if key not in app_before else app_before[key])

    with fast_forward(kubernetes_cluster, ONE_MIN):
        for i, conf in enumerate(pre):
            kubernetes_cluster.config(apps[i], conf)
        kubernetes_cluster.wait(
            lambda status: jubilant.all_active(status, *LOCAL_APPS),
            timeout=timeout * 60,
            error=jubilant.any_error,
        )


def test_nodes_ready(kubernetes_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    apps = [a for a in LOCAL_APPS if a in status.apps]
    units = [u for a in apps for u in status.get_units(a)]
    expected_nodes = sum(len(u) for u in units)
    ready_nodes(kubernetes_cluster, units[0], expected_nodes)


def test_kube_system_pods(kubernetes_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    status = kubernetes_cluster.status()
    k8s = status.apps["k8s"]
    name, _leader = get_leader(k8s)
    wait_pod_phase(kubernetes_cluster, name, None, "Running", namespace="kube-system")
