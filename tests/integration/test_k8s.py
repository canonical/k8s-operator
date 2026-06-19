#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
from pathlib import Path

import jubilant
import pytest
from helpers import fast_forward, get_leader, get_rsc, ready_nodes, wait_pod_phase
from literals import ONE_MIN

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
]

pinned_revision = (
    "latest/edge" not in Path("charms/worker/k8s/templates/snap_installation.yaml").read_text()
)


@pytest.fixture
def preserve_charm_config(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Preserve the charm config changes from a test."""
    apps = ["k8s", "k8s-worker"]
    pre = {app: kubernetes_cluster.config(app) for app in apps}
    yield pre

    with fast_forward(kubernetes_cluster, ONE_MIN):
        for app, before in pre.items():
            current = kubernetes_cluster.config(app)
            # Reset any new config keys added by the test to their default
            to_reset = [key for key in current if key not in before]
            if to_reset:
                kubernetes_cluster.config(app, reset=to_reset)
            if before:
                kubernetes_cluster.config(app, before)
        kubernetes_cluster.wait(
            lambda status: jubilant.all_active(status, *apps), timeout=timeout * 60
        )


def test_nodes_ready(kubernetes_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    expected_nodes = len(status.get_units("k8s")) + len(status.get_units("k8s-worker"))
    k8s_unit = next(iter(status.get_units("k8s")))
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)


def test_kube_system_pods(kubernetes_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    leader = get_leader(kubernetes_cluster, "k8s")
    wait_pod_phase(kubernetes_cluster, leader, None, "Running", namespace="kube-system")


def test_verbose_config(kubernetes_cluster: jubilant.Juju):
    """Test verbose config."""
    status = kubernetes_cluster.status()
    all_units = list(status.get_units("k8s")) + list(status.get_units("k8s-worker"))

    for unit in all_units:
        task = kubernetes_cluster.exec("ps axf | grep kube", unit=unit)
        stdout = task.stdout or ""
        assert all("--v=3" for line in stdout.splitlines() if " /snap/k8s" in line)


@pytest.mark.usefixtures("preserve_charm_config")
def test_nodes_labelled(request, kubernetes_cluster: jubilant.Juju, timeout: int):
    """Test the charms label the nodes appropriately."""
    testname: str = request.node.name
    apps = ["k8s", "k8s-worker"]

    # Set a VALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}="}
    with fast_forward(kubernetes_cluster, ONE_MIN):
        for app in apps:
            kubernetes_cluster.config(app, label_config)
        kubernetes_cluster.wait(
            lambda status: jubilant.all_active(status, *apps), timeout=timeout * 60
        )

    status = kubernetes_cluster.status()
    k8s_unit = next(iter(status.get_units("k8s")))
    total_units = len(status.get_units("k8s")) + len(status.get_units("k8s-worker"))
    nodes = get_rsc(kubernetes_cluster, k8s_unit, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert total_units == len(labelled), "Not all nodes labelled with custom-label"
    assert total_units == len(juju_nodes), "Not all nodes labelled as juju-charms"

    # Set an INVALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}=invalid="}
    leader = get_leader(kubernetes_cluster, "k8s")
    for app in apps:
        kubernetes_cluster.config(app, label_config)
    kubernetes_cluster.wait(jubilant.all_agents_idle, timeout=timeout * 60)
    leader_status = kubernetes_cluster.status().get_units("k8s")[leader].workload_status
    assert leader_status.current == "blocked", "Leader not blocked"
    assert "node-labels" in leader_status.message, "Leader had unexpected warning"

    # Test resetting all label config
    with fast_forward(kubernetes_cluster, ONE_MIN):
        for app in apps:
            kubernetes_cluster.config(app, reset=list(label_config))
        kubernetes_cluster.wait(
            lambda status: jubilant.all_active(status, *apps), timeout=timeout * 60
        )
    nodes = get_rsc(kubernetes_cluster, k8s_unit, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
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
def test_prevent_bootstrap_config_changes(
    kubernetes_cluster: jubilant.Juju, timeout: int, config_key: str, config_value: str
):
    """Test that the bootstrap config cannot be changed."""
    status = kubernetes_cluster.status()
    expected_nodes = len(status.get_units("k8s")) + len(status.get_units("k8s-worker"))
    k8s_unit = next(iter(status.get_units("k8s")))
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)
    kubernetes_cluster.config("k8s", {config_key: config_value})
    kubernetes_cluster.wait(
        lambda status: jubilant.all_blocked(status, "k8s"), timeout=timeout * 60
    )


def test_remove_worker(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    k8s_units = list(status.get_units("k8s"))
    worker_units = list(status.get_units("k8s-worker"))
    expected_nodes = len(k8s_units) + len(worker_units)
    k8s_unit = k8s_units[0]
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)

    # Remove a worker
    log.info("Remove unit %s", worker_units[0])
    kubernetes_cluster.remove_unit(worker_units[0])
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes - 1)
    kubernetes_cluster.add_unit("k8s-worker")
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)


def test_remove_non_leader_control_plane(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    k8s_units = list(status.get_units("k8s"))
    expected_nodes = len(k8s_units) + len(status.get_units("k8s-worker"))
    leader = get_leader(kubernetes_cluster, "k8s")
    leader_idx = k8s_units.index(leader)
    follower = k8s_units[(leader_idx + 1) % len(k8s_units)]
    ready_nodes(kubernetes_cluster, leader, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", follower)
    kubernetes_cluster.remove_unit(follower)
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, leader, expected_nodes - 1)
    kubernetes_cluster.add_unit("k8s")
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, leader, expected_nodes)


def test_remove_leader_control_plane(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    k8s_units = list(status.get_units("k8s"))
    expected_nodes = len(k8s_units) + len(status.get_units("k8s-worker"))
    leader = get_leader(kubernetes_cluster, "k8s")
    leader_idx = k8s_units.index(leader)
    follower = k8s_units[(leader_idx + 1) % len(k8s_units)]
    ready_nodes(kubernetes_cluster, follower, expected_nodes)

    # Remove a control-plane
    log.info("Remove unit %s", leader)
    kubernetes_cluster.remove_unit(leader)
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, follower, expected_nodes - 1)
    kubernetes_cluster.add_unit("k8s")
    kubernetes_cluster.wait(
        lambda status: jubilant.all_active(status, "k8s", "k8s-worker"), timeout=timeout * 60
    )
    ready_nodes(kubernetes_cluster, follower, expected_nodes)


@pytest.mark.skipif(pinned_revision, reason="only run on latest/edge channel")
def test_override_snap_resource(kubernetes_cluster: jubilant.Juju, request):
    """Override the snap resource on a Kubernetes cluster application and revert it after the test.

    This function overrides the snap resource of the "k8s" application in the given
    Kubernetes cluster with a specified override file, waits for the cluster to become idle,
    and then reverts the snap resource back to its original state after the test.

    Args:
        kubernetes_cluster (jubilant.Juju): The Kubernetes cluster juju instance.
        request: The pytest request object containing test configuration options.
    """
    assert "k8s" in kubernetes_cluster.status().apps, "k8s application not found"
    # Override snap resource
    revert = Path(request.config.option.snap_installation_resource)
    override = Path(__file__).parent / "data" / "override-latest-edge.tar.gz"

    try:
        kubernetes_cluster.cli("attach-resource", "k8s", f"snap-installation={override.resolve()}")
        kubernetes_cluster.wait(jubilant.all_active)

        for unit in kubernetes_cluster.status().get_units("k8s").values():
            assert "Override" in unit.workload_status.message
    finally:
        kubernetes_cluster.cli("attach-resource", "k8s", f"snap-installation={revert.resolve()}")
        kubernetes_cluster.wait(jubilant.all_active)
