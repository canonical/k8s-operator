#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
from pathlib import Path

import jubilant
import pytest
from helpers import (
    fast_forward,
    get_leader,
    get_rsc,
    ready_nodes,
    stage,
    unit_names,
    wait_active,
    wait_idle,
    wait_pod_phase,
)
from literals import DEFAULT_DELAY, ONE_MIN, REPO_ROOT, TEST_DATA

log = logging.getLogger(__name__)

APPS = ["k8s", "k8s-worker"]

pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=APPS),
]

# Evaluated at import time, so this must not depend on the current working directory:
# a FileNotFoundError here is a collection error, and the CI job that builds the test
# matrix discards collection output, which would silently drop this whole module.
pinned_revision = (
    "latest/edge"
    not in (REPO_ROOT / "charms/worker/k8s/templates/snap_installation.yaml").read_text()
)


def _expected_nodes(k8s_cluster: jubilant.Juju) -> int:
    """Return the number of Kubernetes nodes the bundle should produce.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.

    Returns:
        The total number of k8s and k8s-worker units.
    """
    status = k8s_cluster.status()
    return sum(len(status.get_units(app)) for app in APPS)


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    ready_nodes(k8s_cluster, get_leader(k8s_cluster, "k8s"), _expected_nodes(k8s_cluster))


def test_kube_system_pods(k8s_cluster: jubilant.Juju):
    """Test that the kube-system pods are running."""
    leader = get_leader(k8s_cluster, "k8s")
    wait_pod_phase(k8s_cluster, leader, None, "Running", namespace="kube-system")


def test_verbose_config(k8s_cluster: jubilant.Juju):
    """Test verbose config."""
    status = k8s_cluster.status()
    all_units = [unit for app in APPS for unit in status.get_units(app)]

    for unit in all_units:
        # No `|| true` guard here on purpose: grep exits non-zero only when nothing
        # matched, and juju.exec turning that into a TaskError is the direct equivalent
        # of the original's `assert rc == 0`.
        stdout = k8s_cluster.exec("ps axf | grep kube", unit=unit).stdout or ""
        # NOTE: this assertion is vacuous -- `all(...)` over a constant string is always
        # true. Carried over verbatim from the pytest-operator suite so the migration
        # stays behaviour-preserving; fixing it is tracked as a follow-up.
        assert all("--v=3" for line in stdout.splitlines() if " /snap/k8s" in line)


@pytest.mark.usefixtures("preserve_charm_config")
def test_nodes_labelled(request: pytest.FixtureRequest, k8s_cluster: jubilant.Juju, timeout: int):
    """Test the charms label the nodes appropriately."""
    testname: str = request.node.name
    expected_nodes = _expected_nodes(k8s_cluster)
    leader = get_leader(k8s_cluster, "k8s")

    # Set a VALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}="}
    with fast_forward(k8s_cluster, ONE_MIN):
        for app in APPS:
            k8s_cluster.config(app, label_config)
        wait_active(k8s_cluster, *APPS, timeout=timeout * 60)

    nodes = get_rsc(k8s_cluster, leader, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    juju_nodes = [n for n in nodes if "juju-charm" in n["metadata"]["labels"]]
    assert expected_nodes == len(labelled), "Not all nodes labelled with custom-label"
    assert expected_nodes == len(juju_nodes), "Not all nodes labelled as juju-charms"

    # Set an INVALID node-label on both k8s and worker
    label_config = {"node-labels": f"{testname}=invalid="}
    for app in APPS:
        k8s_cluster.config(app, label_config)
    wait_idle(k8s_cluster, *APPS, timeout=timeout * 60)
    leader_status = k8s_cluster.status().get_units("k8s")[leader]
    assert leader_status.workload_status.current == "blocked", "Leader not blocked"
    assert "node-labels" in leader_status.workload_status.message, "Leader had unexpected warning"

    # Test resetting all label config
    with fast_forward(k8s_cluster, ONE_MIN):
        for app in APPS:
            k8s_cluster.config(app, reset=list(label_config))
        wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    nodes = get_rsc(k8s_cluster, leader, "nodes")
    labelled = [n for n in nodes if testname in n["metadata"]["labels"]]
    assert 0 == len(labelled), "Not all nodes labelled without custom-label"


def test_remove_worker(k8s_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    expected_nodes = _expected_nodes(k8s_cluster)
    leader = get_leader(k8s_cluster, "k8s")
    ready_nodes(k8s_cluster, leader, expected_nodes)

    victim = unit_names(k8s_cluster, "k8s-worker")[0]
    log.info("Remove unit %s", victim)
    k8s_cluster.remove_unit(victim)
    k8s_cluster.wait(
        lambda status: victim not in status.get_units("k8s-worker"),
        timeout=timeout * 60,
        delay=DEFAULT_DELAY,
    )
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, leader, expected_nodes - 1)

    k8s_cluster.add_unit("k8s-worker")
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, leader, expected_nodes)


def test_remove_non_leader_control_plane(k8s_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    expected_nodes = _expected_nodes(k8s_cluster)
    leader = get_leader(k8s_cluster, "k8s")
    follower = next(unit for unit in unit_names(k8s_cluster, "k8s") if unit != leader)
    ready_nodes(k8s_cluster, leader, expected_nodes)

    log.info("Remove unit %s", follower)
    k8s_cluster.remove_unit(follower)
    k8s_cluster.wait(
        lambda status: follower not in status.get_units("k8s"),
        timeout=timeout * 60,
        delay=DEFAULT_DELAY,
    )
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, leader, expected_nodes - 1)

    k8s_cluster.add_unit("k8s")
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, leader, expected_nodes)


def test_remove_leader_control_plane(k8s_cluster: jubilant.Juju, timeout: int):
    """Deploy the charm and wait for active/idle status."""
    expected_nodes = _expected_nodes(k8s_cluster)
    leader = get_leader(k8s_cluster, "k8s")
    follower = next(unit for unit in unit_names(k8s_cluster, "k8s") if unit != leader)
    ready_nodes(k8s_cluster, follower, expected_nodes)

    log.info("Remove unit %s", leader)
    k8s_cluster.remove_unit(leader)
    k8s_cluster.wait(
        lambda status: leader not in status.get_units("k8s"),
        timeout=timeout * 60,
        delay=DEFAULT_DELAY,
    )
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, follower, expected_nodes - 1)

    k8s_cluster.add_unit("k8s")
    wait_active(k8s_cluster, *APPS, timeout=timeout * 60)
    ready_nodes(k8s_cluster, follower, expected_nodes)


@pytest.mark.skipif(pinned_revision, reason="only run on latest/edge channel")
def test_override_snap_resource(
    k8s_cluster: jubilant.Juju, request: pytest.FixtureRequest, timeout: int
):
    """Override the snap resource on the k8s application and revert it after the test."""
    # The juju CLI opens these paths itself, so they must be readable by the juju snap.
    module = request.module.__name__
    revert = stage(Path(request.config.option.snap_installation_resource), module)
    override = stage(TEST_DATA / "override-latest-edge.tar.gz", module)

    try:
        k8s_cluster.cli("attach-resource", "k8s", f"snap-installation={override}")
        wait_active(k8s_cluster, timeout=timeout * 60)
        for unit, unit_status in k8s_cluster.status().get_units("k8s").items():
            assert "Override" in unit_status.workload_status.message, (
                f"Unit {unit} missing 'Override' in its status message"
            )
    finally:
        k8s_cluster.cli("attach-resource", "k8s", f"snap-installation={revert}")
        wait_active(k8s_cluster, timeout=timeout * 60)
