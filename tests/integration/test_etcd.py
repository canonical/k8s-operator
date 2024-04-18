#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import pytest
from juju import model, unit

from .helpers import ready_nodes

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle_file("test-bundle-etcd.yaml"),
    pytest.mark.ignore_blocked,
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
    etcd: unit.Unit = kubernetes_cluster.applications["etcd"].units[0]
    etcd_port = etcd.safe_data["ports"][0]["number"]
    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert status["datastore"]["servers"] == [
        f"https://{etcd.public_address}:{etcd_port}"
    ]
