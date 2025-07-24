#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import pytest
from helpers import ready_nodes
from juju import model, unit

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-charmed-etcd.yaml", apps_local=["k8s", "k8s-worker"])
]


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


@pytest.mark.abort_on_fail
async def test_etcd_datastore(kubernetes_cluster: model.Model):
    """Test that etcd is the backend datastore."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    etcd: unit.Unit = kubernetes_cluster.applications["charmed-etcd"].units[0]
    etcd_port = etcd.safe_data["ports"][0]["number"]
    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert f"https://{etcd.public_address}:{etcd_port}" in status["datastore"]["servers"]


@pytest.mark.abort_on_fail
async def test_update_etcd_cluster(kubernetes_cluster: model.Model):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    etcd = kubernetes_cluster.applications["charmed-etcd"]
    count = 3 - len(etcd.units)
    if count > 0:
        await etcd.add_unit(count=count)
    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)

    expected_servers = []
    for u in etcd.units:
        etcd_port = u.safe_data["ports"][0]["number"]
        expected_servers.append(f"https://{u.public_address}:{etcd_port}")

    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(status["datastore"]["servers"]) == set(expected_servers)
