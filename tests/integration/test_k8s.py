#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import json
import logging

import pytest
from juju import model
from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


async def get_nodes(k8s):
    """Return Node list

    Args:
        k8s: any k8s unit

    Returns:
        list of nodes
    """
    action = await k8s.run("k8s kubectl get nodes -o json")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get nodes with kubectl"
    log.info("Parsing node list...")
    node_list = json.loads(result.results["stdout"])
    assert node_list["kind"] == "List", "Should have found a list of nodes"
    return node_list["items"]


@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(15))
async def ready_nodes(k8s, expected_count):
    """Get a list of the ready nodes.

    Args:
        k8s: k8s unit
        expected_count: number of expected nodes
    """
    log.info("Finding all nodes...")
    nodes = await get_nodes(k8s)
    ready_nodes = {
        node["metadata"]["name"]: all(
            condition["status"] == "False"
            for condition in node["status"]["conditions"]
            if condition["type"] != "Ready"
        )
        for node in nodes
    }
    log.info("Found %d/%d nodes...", len(ready_nodes), expected_count)
    assert len(ready_nodes) == expected_count, f"Expect {expected_count} nodes in the list"
    for node, ready in ready_nodes.items():
        log.info("Node %s is %s..", node, "ready" if ready else "not ready")
        assert ready, f"Node not yet ready: {node}."


async def get_leader(app):
    """Find leader unit of an application.

    Args:
        app: Juju application

    Returns:
        int: index to leader unit
    """
    is_leader = await asyncio.gather(*(u.is_leader_from_status() for u in app.units))
    for idx, flag in enumerate(is_leader):
        if flag:
            return idx


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_nodes_labelled(kubernetes_cluster):
    """Test the charms label the nodes appropriately."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    nodes = await get_nodes(k8s.units[0])
    control_plane_label = "node-role.kubernetes.io/control-plane"
    control_plane = [n for n in nodes if control_plane_label in n["metadata"]["labels"]]
    assert len(k8s.units) == len(control_plane), "Not all control-plane nodes labeled"
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert len(k8s.units + worker.units) == len(juju_nodes), "Not all nodes labeled as juju-charms"


@pytest.mark.abort_on_fail
async def test_remove_worker(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)

    # Remove a worker
    log.info("Remove unit %s", worker.units[0].name)
    await worker.units[0].destroy()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(k8s.units[0], expected_nodes - 1)
    await worker.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(k8s.units[0], expected_nodes)


@pytest.mark.abort_on_fail
async def test_remove_non_leader_control_plane(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    follower = k8s.units[(leader_idx + 1) % len(k8s.units)]
    await ready_nodes(leader, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", follower.name)
    await follower.destroy()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(leader, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(leader, expected_nodes)


@pytest.mark.abort_on_fail
async def test_remove_leader_control_plane(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    follower = k8s.units[(leader_idx + 1) % len(k8s.units)]
    await ready_nodes(follower, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", leader.name)
    await leader.destroy()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(follower, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=3 * 60)
    await ready_nodes(follower, expected_nodes)
