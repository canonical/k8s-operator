#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging

import juju.application
import juju.model
import juju.unit
import pytest

from .helpers import ready_nodes

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-config-changes.yaml", apps_local=["k8s", "k8s-worker"]),
]


async def test_prevent_bootstrap_config_changes(kubernetes_cluster: juju.model.Model):
    """Test that the bootstrap config cannot be changed."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)
    new_config = {"bootstrap-node-taints": "new-taint"}
    await asyncio.gather(k8s.set_config(new_config), worker.set_config(new_config))
    await kubernetes_cluster.wait_for_idle(timeout=5 * 60)
    assert k8s.units[0].workload_status == "blocked", (
        f"Control plane should be blocked, but is {k8s.units[0].workload_status}"
    )
    assert worker.units[0].workload_status == "blocked", (
        f"Worker should be blocked, but is {worker.units[0].workload_status}"
    )
