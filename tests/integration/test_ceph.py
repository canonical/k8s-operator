#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import pytest
from juju import model, unit

from .helpers import ready_nodes

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
pytestmark = [
    pytest.mark.bundle_file("test-bundle-ceph.yaml"),
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
async def test_ceph_sc(kubernetes_cluster: model.Model):
    """Test that a ceph storage class is available."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    event = await k8s.run("k8s kubectl get sc -o=jsonpath='{.items[*].provisioner}'")
    result = await event.wait()
    stdout = result.results["stdout"]
    assert "rbd.csi.ceph.com" in stdout, f"No ceph provisioner found in: {stdout}"
