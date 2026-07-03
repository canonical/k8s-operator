#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the k8s charm (Jubilant-based)."""

import logging
from pathlib import Path

import jubilant
import pytest
from conftest import fast_forward
from helpers import get_leader, get_rsc, get_unit_names, ready_nodes, wait_pod_phase
from literals import ONE_MIN

log = logging.getLogger(__name__)


pinned_revision = (
    "latest/edge" not in Path("charms/worker/k8s/templates/snap_installation.yaml").read_text()
)

APPS = ["k8s", "k8s-worker"]


# Module fixture


@pytest.fixture
def preserve_charm_config(k8s_cluster: jubilant.Juju, timeout: int):
    """Snapshot charm config before a test and restore it afterwards.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        timeout:     Timeout in minutes for the idle wait.
    """
    pre = {app: k8s_cluster.config(app) for app in APPS}
    yield pre

    post = {app: k8s_cluster.config(app) for app in APPS}

    for app in APPS:
        reset_keys = []
        set_values = {}
        for key, meta in post[app].items():
            if key not in pre[app]:
                # Key was added by the test — reset to default.
                reset_keys.append(key)
            else:
                set_values[key] = pre[app][key]

        if reset_keys:
            k8s_cluster.config(app, reset=reset_keys)
        if set_values:
            k8s_cluster.config(app, set_values)

    with fast_forward(k8s_cluster, ONE_MIN):
        k8s_cluster.wait(
            lambda s: jubilant.all_active(s, *APPS),
            timeout=timeout * 60,
        )


# Tests


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """All k8s and k8s-worker nodes are Ready."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.apps[a].units) for a in APPS)
    leader = get_leader(k8s_cluster, "k8s")
    ready_nodes(k8s_cluster, leader, expected_nodes)


def test_kube_system_pods(k8s_cluster: jubilant.Juju):
    """All kube-system pods are Running."""
    leader = get_leader(k8s_cluster, "k8s")
    wait_pod_phase(k8s_cluster, leader, None, "Running", namespace="kube-system")


def test_verbose_config(k8s_cluster: jubilant.Juju):
    """All k8s component processes on every unit use --v=3."""
    status = k8s_cluster.status()
    all_units = list(status.apps["k8s"].units) + list(status.apps["k8s-worker"].units)

    for unit_name in all_units:
        task = k8s_cluster.exec("ps axf | grep kube", unit=unit_name)
        stdout = task.stdout or ""
        assert all(
            "--v=3" in line for line in stdout.splitlines() if " /snap/k8s" in line
        ), f"Expected --v=3 in kube processes on {unit_name}"


@pytest.mark.usefixtures("preserve_charm_config")
def test_nodes_labelled(
    request: pytest.FixtureRequest,
    k8s_cluster: jubilant.Juju,
    timeout: int,
):
    """The charms label nodes appropriately."""
    testname: str = request.node.name
    status = k8s_cluster.status()
    all_unit_count = sum(len(status.apps[a].units) for a in APPS)

    # Set a VALID node-label on both k8s and k8s-worker.
    label_config = {"node-labels": f"{testname}="}
    with fast_forward(k8s_cluster, ONE_MIN):
        for app in APPS:
            k8s_cluster.config(app, label_config)
        k8s_cluster.wait(
            lambda s: jubilant.all_active(s, *APPS),
            timeout=timeout * 60,
        )

    leader = get_leader(k8s_cluster, "k8s")
    nodes = get_rsc(k8s_cluster, leader, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert len(labelled) == all_unit_count, "Not all nodes labelled with custom-label"
    assert len(juju_nodes) == all_unit_count, "Not all nodes labelled as juju-charms"

    # Set an INVALID node-label.
    invalid_config = {"node-labels": f"{testname}=invalid="}
    for app in APPS:
        k8s_cluster.config(app, invalid_config)
    k8s_cluster.wait(
        lambda s: jubilant.all_blocked(s, "k8s"),
        timeout=timeout * 60,
    )
    leader_status = k8s_cluster.status().apps["k8s"].units[leader]
    assert leader_status.workload_status.current == "blocked", "Leader not blocked"
    assert "node-labels" in (leader_status.workload_status.message or ""), (
        "Leader had unexpected warning"
    )

    # Reset label config.
    with fast_forward(k8s_cluster, ONE_MIN):
        for app in APPS:
            k8s_cluster.config(app, reset=list(invalid_config))
        k8s_cluster.wait(
            lambda s: jubilant.all_active(s, *APPS),
            timeout=timeout * 60,
        )
    nodes = get_rsc(k8s_cluster, leader, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    assert len(labelled) == 0, "Nodes still labelled after reset"


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
    k8s_cluster: jubilant.Juju,
    timeout: int,
    config_key: str,
    config_value: str,
):
    """Bootstrap configuration keys must not be changeable post-deploy."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.apps[a].units) for a in APPS)
    leader = get_leader(k8s_cluster, "k8s")
    ready_nodes(k8s_cluster, leader, expected_nodes)

    k8s_cluster.config("k8s", {config_key: config_value})
    k8s_cluster.wait(
        lambda s: jubilant.all_blocked(s, "k8s"),
        timeout=timeout * 60,
    )


def test_remove_worker(k8s_cluster: jubilant.Juju, timeout: int):
    """Remove a worker unit and verify the node count decreases, then restore."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.apps[a].units) for a in APPS)
    leader = get_leader(k8s_cluster, "k8s")
    ready_nodes(k8s_cluster, leader, expected_nodes)

    worker_units = get_unit_names(k8s_cluster, "k8s-worker")
    victim = worker_units[0]
    log.info("Removing worker unit %s", victim)
    k8s_cluster.remove_unit(victim)
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, leader, expected_nodes - 1)

    k8s_cluster.add_unit("k8s-worker")
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, leader, expected_nodes)


def test_remove_non_leader_control_plane(k8s_cluster: jubilant.Juju, timeout: int):
    """Remove a non-leader control-plane unit and verify the cluster recovers."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.apps[a].units) for a in APPS)
    leader = get_leader(k8s_cluster, "k8s")
    ready_nodes(k8s_cluster, leader, expected_nodes)

    k8s_units = get_unit_names(k8s_cluster, "k8s")
    follower = next(u for u in k8s_units if u != leader)
    log.info("Removing non-leader control-plane unit %s", follower)
    k8s_cluster.remove_unit(follower)
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, leader, expected_nodes - 1)

    k8s_cluster.add_unit("k8s")
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, leader, expected_nodes)


def test_remove_leader_control_plane(k8s_cluster: jubilant.Juju, timeout: int):
    """Remove the leader control-plane unit and verify the cluster recovers."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.apps[a].units) for a in APPS)
    leader = get_leader(k8s_cluster, "k8s")

    k8s_units = get_unit_names(k8s_cluster, "k8s")
    follower = next(u for u in k8s_units if u != leader)
    ready_nodes(k8s_cluster, follower, expected_nodes)

    log.info("Removing leader control-plane unit %s", leader)
    k8s_cluster.remove_unit(leader)
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, follower, expected_nodes - 1)

    k8s_cluster.add_unit("k8s")
    k8s_cluster.wait(
        lambda s: jubilant.all_active(s, *APPS),
        timeout=timeout * 60,
    )
    ready_nodes(k8s_cluster, follower, expected_nodes)


@pytest.mark.skipif(pinned_revision, reason="only run on latest/edge channel")
def test_override_snap_resource(
    k8s_cluster: jubilant.Juju,
    request: pytest.FixtureRequest,
    timeout: int,
):
    """Override the snap-installation resource and verify, then revert.

    Args:
        k8s_cluster: Jubilant Juju instance.
        request:     Pytest request (carries --snap-installation-resource).
        timeout:     Timeout in minutes.
    """
    revert = Path(request.config.option.snap_installation_resource)
    data_dir = Path(__file__).parent.parent / "integration" / "data"
    override = data_dir / "override-latest-edge.tar.gz"

    try:
        k8s_cluster.cli("attach-resource", "k8s", f"snap-installation={override}")
        k8s_cluster.wait(
            lambda s: jubilant.all_active(s, "k8s"),
            timeout=timeout * 60,
        )
        status = k8s_cluster.status()
        for unit_name, unit_st in status.apps["k8s"].units.items():
            assert "Override" in (unit_st.workload_status.message or ""), (
                f"Unit {unit_name} missing 'Override' in status message"
            )
    finally:
        k8s_cluster.cli("attach-resource", "k8s", f"snap-installation={revert}")
        k8s_cluster.wait(
            lambda s: jubilant.all_active(s, "k8s"),
            timeout=timeout * 60,
        )
