#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging

import pytest
from juju import application, model
from tenacity import retry, stop_after_attempt, wait_fixed

from .grafana import Grafana
from .helpers import get_nodes, ready_nodes
from .prometheus import Prometheus

log = logging.getLogger(__name__)


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


async def test_nodes_labelled(request, kubernetes_cluster: model.Model):
    """Test the charms label the nodes appropriately."""
    testname: str = request.node.name
    k8s: application.Application = kubernetes_cluster.applications["k8s"]
    worker: application.Application = kubernetes_cluster.applications["k8s-worker"]
    label_config = {"labels": f"{testname}="}
    await asyncio.gather(k8s.set_config(label_config), worker.set_config(label_config))
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)

    try:
        nodes = await get_nodes(k8s.units[0])
        labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
        juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
        assert len(k8s.units + worker.units) == len(
            labelled
        ), "Not all nodes labelled with custom-label"
        assert len(k8s.units + worker.units) == len(
            juju_nodes
        ), "Not all nodes labelled as juju-charms"
    finally:
        await asyncio.gather(
            k8s.reset_config(list(label_config)), worker.reset_config(list(label_config))
        )

    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
    nodes = await get_nodes(k8s.units[0])
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert 0 == len(labelled), "Not all nodes labelled with custom-label"


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
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
    await ready_nodes(k8s.units[0], expected_nodes - 1)
    await worker.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
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
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
    await ready_nodes(leader, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
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
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
    await ready_nodes(follower, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)
    await ready_nodes(follower, expected_nodes)


@pytest.mark.cos
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
async def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: model.Model,
):
    """Test integration with Grafana."""
    grafana = Grafana(model_name=cos_model.name, base=traefik_url, password=grafana_password)
    await asyncio.wait_for(grafana.is_ready(), timeout=10 * 60)
    dashboards = await grafana.dashboards_all()
    actual_dashboard_titles = set()

    for dashboard in dashboards:
        actual_dashboard_titles.add(dashboard.get("title"))

    assert expected_dashboard_titles.issubset(actual_dashboard_titles)


@pytest.mark.cos
@pytest.mark.usefixtures("related_prometheus")
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
async def test_prometheus(traefik_url: str, cos_model: model.Model):
    """Test integration with Prometheus."""
    prometheus = Prometheus(model_name=cos_model.name, base=traefik_url)
    await asyncio.wait_for(prometheus.is_ready(), timeout=10 * 60)

    queries = [
        'up{job="kubelet", metrics_path="/metrics"} > 0',
        'up{job="kubelet", metrics_path="/metrics/cadvisor"} > 0',
        'up{job="kubelet", metrics_path="/metrics/probes"} > 0',
        'up{job="apiserver"} > 0',
        'up{job="kube-controller-manager"} > 0',
        'up{job="kube-scheduler"} > 0',
        'up{job="kube-proxy"} > 0',
        'up{job="kube-state-metrics"} > 0',
    ]
    results = await asyncio.gather(*[prometheus.get_metrics(query) for query in queries])
    failed = [query for query, result in zip(queries, results) if not result]
    assert not failed, f"Failed queries: {failed}"
