#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import jubilant
import pytest
from helpers import ready_nodes

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [pytest.mark.bundle(file="test-bundle-etcd.yaml", apps_local=["k8s", "k8s-worker"])]


def _etcd_port(unit) -> int:
    """Return the first opened port number of a unit (e.g. "2379/tcp" -> 2379)."""
    return int(unit.open_ports[0].split("/")[0])


@pytest.mark.abort_on_fail
def test_nodes_ready(kubernetes_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    expected_nodes = len(status.get_units("k8s")) + len(status.get_units("k8s-worker"))
    k8s_unit = next(iter(status.get_units("k8s")))
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)


@pytest.mark.abort_on_fail
def test_etcd_datastore(kubernetes_cluster: jubilant.Juju):
    """Test that etcd is the backend datastore."""
    status = kubernetes_cluster.status()
    k8s_unit = next(iter(status.get_units("k8s")))
    etcd_unit = next(iter(status.get_units("etcd").values()))
    etcd_port = _etcd_port(etcd_unit)
    task = kubernetes_cluster.exec("k8s status --output-format json", unit=k8s_unit)
    cluster_status = json.loads(task.stdout)
    assert cluster_status["ready"], "Cluster isn't ready"
    assert cluster_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert (
        f"https://{etcd_unit.public_address}:{etcd_port}" in cluster_status["datastore"]["servers"]
    )


@pytest.mark.abort_on_fail
def test_update_etcd_cluster(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    etcd_units = kubernetes_cluster.status().get_units("etcd")
    count = 3 - len(etcd_units)
    if count > 0:
        kubernetes_cluster.add_unit("etcd", num_units=count)
    at_least_twenty = max(20, timeout)
    kubernetes_cluster.wait(jubilant.all_active, timeout=at_least_twenty * 60)

    status = kubernetes_cluster.status()
    expected_servers = []
    for u in status.get_units("etcd").values():
        expected_servers.append(f"https://{u.public_address}:{_etcd_port(u)}")

    k8s_unit = next(iter(status.get_units("k8s")))
    task = kubernetes_cluster.exec("k8s status --output-format json", unit=k8s_unit)
    cluster_status = json.loads(task.stdout)
    assert cluster_status["ready"], "Cluster isn't ready"
    assert cluster_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(cluster_status["datastore"]["servers"]) == set(expected_servers)
