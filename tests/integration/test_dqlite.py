#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import jubilant
import pytest
from helpers import get_leader, ready_nodes, unit_names, wait_pod_phase

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with dqlite, for all the test within this module.
APPS = ["k8s", "k8s-worker"]
pytestmark = [pytest.mark.bundle(file="test-bundle-dqlite.yaml", apps_local=APPS)]


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.get_units(app)) for app in APPS)
    ready_nodes(k8s_cluster, get_leader(k8s_cluster, "k8s"), expected_nodes)


def test_check_right_datastore_config(k8s_cluster: jubilant.Juju):
    """Test that the bootstrap config is set correctly for dqlite."""
    unit = unit_names(k8s_cluster, "k8s")[0]
    status = json.loads(k8s_cluster.exec("k8s status --output-format json", unit=unit).stdout)
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "k8s-dqlite", "Datastore type is not set to dqlite"


def test_kube_system_pods(k8s_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    leader = get_leader(k8s_cluster, "k8s")
    wait_pod_phase(k8s_cluster, leader, None, "Running", namespace="kube-system")
