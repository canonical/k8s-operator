#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging
from pathlib import Path

import juju.application
import juju.model
import juju.unit
import pytest
import pytest_asyncio
from tenacity import retry, stop_after_attempt, wait_fixed

from .grafana import Grafana
from .helpers import get_leader, get_rsc, ready_nodes, wait_pod_phase
from .prometheus import Prometheus

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
]


@pytest_asyncio.fixture
async def preserve_charm_config(kubernetes_cluster: juju.model.Model):
    """Preserve the charm config changes from a test."""
    k8s: juju.application.Application = kubernetes_cluster.applications["k8s"]
    worker: juju.application.Application = kubernetes_cluster.applications["k8s-worker"]
    k8s_config, worker_config = await asyncio.gather(k8s.get_config(), worker.get_config())
    yield k8s_config, worker_config
    await asyncio.gather(k8s.set_config(k8s_config), worker.set_config(worker_config))
    await kubernetes_cluster.wait_for_idle(status="active", timeout=10 * 60)


async def test_nodes_ready(kubernetes_cluster: juju.model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_kube_system_pods(kubernetes_cluster: juju.model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    await wait_pod_phase(leader, None, "Running", namespace="kube-system")


async def test_verbose_config(kubernetes_cluster: juju.model.Model):
    """Test verbose config."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    all_units = k8s.units + worker.units

    unit_events = await asyncio.gather(*(u.run("ps axf | grep kube") for u in all_units))
    unit_runs = await asyncio.gather(*(u.wait() for u in unit_events))
    for idx, unit_run in enumerate(unit_runs):
        rc, stdout, stderr = (
            unit_run.results["return-code"],
            unit_run.results.get("stdout") or "",
            unit_run.results.get("stderr") or "",
        )
        assert rc == 0, f"Failed to run 'ps axf' on {all_units[idx].name}: {stderr}"
        assert all("--v=3" for line in stdout.splitlines() if " /snap/k8s" in line)


@pytest.mark.usefixtures("preserve_charm_config")
async def test_nodes_labelled(request, kubernetes_cluster: juju.model.Model):
    """Test the charms label the nodes appropriately."""
    testname: str = request.node.name
    k8s: juju.application.Application = kubernetes_cluster.applications["k8s"]
    worker: juju.application.Application = kubernetes_cluster.applications["k8s-worker"]

    # Set a VALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}="}
    await asyncio.gather(k8s.set_config(label_config), worker.set_config(label_config))
    await kubernetes_cluster.wait_for_idle(status="active", timeout=5 * 60)

    nodes = await get_rsc(k8s.units[0], "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert len(k8s.units + worker.units) == len(
        labelled
    ), "Not all nodes labelled with custom-label"
    assert len(k8s.units + worker.units) == len(
        juju_nodes
    ), "Not all nodes labelled as juju-charms"

    # Set an INVALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}=invalid="}
    await asyncio.gather(k8s.set_config(label_config), worker.set_config(label_config))
    await kubernetes_cluster.wait_for_idle(timeout=5 * 60)
    leader_idx = await get_leader(k8s)
    leader: juju.unit.Unit = k8s.units[leader_idx]
    assert leader.workload_status == "blocked", "Leader not blocked"
    assert "node-labels" in leader.workload_status_message, "Leader had unexpected warning"

    # Test resetting all label config
    await asyncio.gather(
        k8s.reset_config(list(label_config)), worker.reset_config(list(label_config))
    )
    await kubernetes_cluster.wait_for_idle(status="active", timeout=5 * 60)
    nodes = await get_rsc(k8s.units[0], "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert 0 == len(labelled), "Not all nodes labelled without custom-label"


async def test_remove_worker(kubernetes_cluster: juju.model.Model):
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


async def test_remove_non_leader_control_plane(kubernetes_cluster: juju.model.Model):
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


async def test_remove_leader_control_plane(kubernetes_cluster: juju.model.Model):
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


async def test_override_snap_resource(kubernetes_cluster: juju.model.Model, request):
    """Override the snap resource on a Kubernetes cluster application and revert it after the test.

    This function overrides the snap resource of the "k8s" application in the given
    Kubernetes cluster with a specified override file, waits for the cluster to become idle,
    and then reverts the snap resource back to its original state after the test.

    Args:
        kubernetes_cluster (model.Model): The Kubernetes cluster model.
        request: The pytest request object containing test configuration options.
    """
    k8s = kubernetes_cluster.applications["k8s"]
    assert k8s, "k8s application not found"
    # Override snap resource
    revert = Path(request.config.option.snap_installation_resource)
    override = Path(__file__).parent / "data" / "override-latest-edge.tar.gz"

    try:
        with override.open("rb") as obj:
            k8s.attach_resource("snap-installation", override, obj)
            await kubernetes_cluster.wait_for_idle(status="active", idle_period=30)

        for _unit in k8s.units:
            assert "Override" in _unit.workload_status_message
    finally:
        with revert.open("rb") as obj:
            k8s.attach_resource("snap-installation", revert, obj)
            await kubernetes_cluster.wait_for_idle(status="active")


@pytest.mark.cos
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
async def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: juju.model.Model,
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
async def test_prometheus(traefik_url: str, cos_model: juju.model.Model):
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
