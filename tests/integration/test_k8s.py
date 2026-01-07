#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging
from pathlib import Path

import juju.model
import juju.unit
import pytest
import pytest_asyncio
from helpers import get_leader, get_rsc, ready_nodes, wait_pod_phase
from literals import ONE_MIN

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
]

pinned_revision = (
    "latest/edge" not in Path("charms/worker/k8s/templates/snap_installation.yaml").read_text()
)


@pytest_asyncio.fixture
async def preserve_charm_config(ops_test, kubernetes_cluster: juju.model.Model, timeout: int):
    """Preserve the charm config changes from a test."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
    pre = await asyncio.gather(k8s.get_config(), worker.get_config())
    yield pre
    post = await asyncio.gather(k8s.get_config(), worker.get_config())

    for app_before, app_after in zip(pre, post):
        for key in app_after.keys():
            # Reset any new config keys added by the test to their default
            app_before[key] = str(
                app_after[key]["default"] if key not in app_before else app_before[key]["value"]
            )

    async with ops_test.fast_forward(ONE_MIN):
        await asyncio.gather(k8s.set_config(pre[0]), worker.set_config(pre[1]))
        await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)


async def test_nodes_ready(kubernetes_cluster: juju.model.Model):
    """Deploy the charm and wait for active/idle status."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
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
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
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
async def test_nodes_labelled(
    request, ops_test, kubernetes_cluster: juju.model.Model, timeout: int
):
    """Test the charms label the nodes appropriately."""
    testname: str = request.node.name
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)

    # Set a VALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}="}
    async with ops_test.fast_forward(ONE_MIN):
        await asyncio.gather(k8s.set_config(label_config), worker.set_config(label_config))
        await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)

    nodes = await get_rsc(k8s.units[0], "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert len(k8s.units + worker.units) == len(labelled), (
        "Not all nodes labelled with custom-label"
    )
    assert len(k8s.units + worker.units) == len(juju_nodes), (
        "Not all nodes labelled as juju-charms"
    )

    # Set an INVALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}=invalid="}
    leader_idx = await get_leader(k8s)
    await asyncio.gather(k8s.set_config(label_config), worker.set_config(label_config))
    await kubernetes_cluster.wait_for_idle(apps=apps, timeout=timeout * 60)
    leader: juju.unit.Unit = k8s.units[leader_idx]
    assert leader.workload_status == "blocked", "Leader not blocked"
    assert "node-labels" in leader.workload_status_message, "Leader had unexpected warning"

    # Test resetting all label config
    async with ops_test.fast_forward(ONE_MIN):
        await asyncio.gather(
            k8s.reset_config(list(label_config)), worker.reset_config(list(label_config))
        )
        await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    nodes = await get_rsc(k8s.units[0], "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert 0 == len(labelled), "Not all nodes labelled without custom-label"


@pytest.mark.usefixtures("preserve_charm_config")
@pytest.mark.parametrize(
    "config_key, config_value",
    [
        ("bootstrap-pod-cidr", "10.0.0.0/8"),
        ("bootstrap-service-cidr", "10.128.0.0/16"),
        ("bootstrap-datastore", "etcd"),
    ],
)
async def test_prevent_bootstrap_config_changes(
    kubernetes_cluster: juju.model.Model, timeout: int, config_key: str, config_value: str
):
    """Test that the bootstrap config cannot be changed."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)
    await k8s.set_config({config_key: config_value})
    await kubernetes_cluster.wait_for_idle(apps=apps[:1], status="blocked", timeout=timeout * 60)


async def test_remove_worker(kubernetes_cluster: juju.model.Model, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)

    # Remove a worker
    log.info("Remove unit %s", worker.units[0].name)
    await worker.units[0].destroy()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(k8s.units[0], expected_nodes - 1)
    await worker.add_unit()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_remove_non_leader_control_plane(kubernetes_cluster: juju.model.Model, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
    expected_nodes = len(k8s.units) + len(worker.units)
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    follower = k8s.units[(leader_idx + 1) % len(k8s.units)]
    await ready_nodes(leader, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", follower.name)
    await follower.destroy()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(leader, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(leader, expected_nodes)


async def test_remove_leader_control_plane(kubernetes_cluster: juju.model.Model, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    apps = ["k8s", "k8s-worker"]
    k8s, worker = (kubernetes_cluster.applications[a] for a in apps)
    expected_nodes = len(k8s.units) + len(worker.units)
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    follower = k8s.units[(leader_idx + 1) % len(k8s.units)]
    await ready_nodes(follower, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", leader.name)
    await leader.destroy()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(follower, expected_nodes - 1)
    await k8s.add_unit()
    await kubernetes_cluster.wait_for_idle(apps=apps, status="active", timeout=timeout * 60)
    await ready_nodes(follower, expected_nodes)


@pytest.mark.skipif(pinned_revision, reason="only run on latest/edge channel")
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
