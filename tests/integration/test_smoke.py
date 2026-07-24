#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging

import jubilant
import pytest
from helpers import get_leader, ready_nodes, wait_pod_phase

log = logging.getLogger(__name__)

APPS = ["k8s"]

pytestmark = [
    pytest.mark.bundle(file="test-smoke.yaml", apps_local=APPS),
]


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.get_units(app)) for app in APPS)
    ready_nodes(k8s_cluster, get_leader(k8s_cluster, "k8s"), expected_nodes)


def test_kube_system_pods(k8s_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    leader = get_leader(k8s_cluster, "k8s")
    wait_pod_phase(k8s_cluster, leader, None, "Running", namespace="kube-system")
