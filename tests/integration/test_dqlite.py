#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import pytest
from juju import model, unit

from .helpers import get_leader, ready_nodes, wait_pod_phase

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with managed etcd, for all the test within this module.
pytestmark = [pytest.mark.bundle(file="test-bundle-dqlite.yaml", apps_local=["k8s", "k8s-worker"])]


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_check_right_datastore_config(kubernetes_cluster: model.Model):
    """Test that the bootstrap config is set correctly for dqlite."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "k8s-dqlite", "Datastore type is not set to dqlite"


async def test_kube_system_pods(kubernetes_cluster: model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    await wait_pod_phase(leader, None, "Running", namespace="kube-system")
