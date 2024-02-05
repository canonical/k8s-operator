#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json
import logging

import pytest
from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


@retry(reraise=True, stop=stop_after_attempt(10), wait=wait_fixed(15))
async def ready_nodes(k8s, expected_count):
    """Get a list of the ready nodes.

    Args:
        k8s: k8s unit
        expected_count: number of expected nodes

    Returns:
        list of nodes
    """
    action = await k8s.run("k8s kubectl get nodes -o json")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get nodes with kubectl"
    node_list = json.loads(result.results["stdout"])
    assert node_list["kind"] == "List", "Should have found a list of nodes"
    nodes = [
        node
        for node in node_list["items"]
        if all(
            condition["status"] == "False"
            for condition in node["status"]["conditions"]
            if condition["type"] != "Ready"
        )
    ]
    assert len(nodes) == expected_count, f"Expect {expected_count} nodes in the list"
    return nodes


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    await ready_nodes(k8s.units[0], 3)
