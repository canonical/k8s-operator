#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json
import logging

import pytest
from juju import application, model
from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(15))
async def ready_nodes(k8s, expected_count):
    """Get a list of the ready nodes.

    Args:
        k8s: k8s unit
        expected_count: number of expected nodes

    Returns:
        list of nodes
    """
    log.info("Finding all nodes...")
    action = await k8s.run("k8s kubectl get nodes -o json")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get nodes with kubectl"
    log.info("Parsing node list...")
    node_list = json.loads(result.results["stdout"])
    assert node_list["kind"] == "List", "Should have found a list of nodes"
    nodes = {
        node["metadata"]["name"]: all(
            condition["status"] == "False"
            for condition in node["status"]["conditions"]
            if condition["type"] != "Ready"
        )
        for node in node_list["items"]
    }
    log.info("Found %d/%d nodes...", len(nodes), expected_count)
    assert len(nodes) == expected_count, f"Expect {expected_count} nodes in the list"
    for node, ready in nodes.items():
        log.info("Node %s is %s..", node, "ready" if ready else "not ready")
        assert ready, f"Node not yet ready: {node}."
    return nodes


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
<<<<<<< HEAD
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)
=======
    await ready_nodes(k8s.units[0], 3)


@pytest.mark.abort_on_fail
async def test_remove_units(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s: application.Application = kubernetes_cluster.applications["k8s"]
    await ready_nodes(k8s.units[0], 3)

    kubernetes_cluster.destroy_unit(k8s.units[1])
    await ready_nodes(k8s.units[0], 2)
    k8s.destroy_relation("k8s", "k8s-worker:cluster")
    await ready_nodes(k8s.units[0], 1)
>>>>>>> 5e2fa06 (use set and add integration test)
