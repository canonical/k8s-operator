#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json

import jubilant
import pytest
from helpers import get_leader, ready_nodes, unit_names, unit_port, wait_active

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
APPS = ["k8s", "k8s-worker"]
pytestmark = [pytest.mark.bundle(file="test-bundle-etcd.yaml", apps_local=APPS)]


def _etcd_servers(status: jubilant.Status) -> set:
    """Return the expected etcd client URLs for every etcd unit.

    Args:
        status: A Juju status object.

    Returns:
        Set of ``https://<address>:<port>`` strings.
    """
    return {
        f"https://{unit.public_address}:{unit_port(status, 'etcd', name)}"
        for name, unit in status.get_units("etcd").items()
    }


def _k8s_status(k8s_cluster: jubilant.Juju) -> dict:
    """Return the parsed output of ``k8s status`` on the first k8s unit.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.

    Returns:
        The parsed ``k8s status`` output.
    """
    unit = unit_names(k8s_cluster, "k8s")[0]
    return json.loads(k8s_cluster.exec("k8s status --output-format json", unit=unit).stdout)


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.get_units(app)) for app in APPS)
    ready_nodes(k8s_cluster, get_leader(k8s_cluster, "k8s"), expected_nodes)


def test_etcd_datastore(k8s_cluster: jubilant.Juju):
    """Test that etcd is the backend datastore."""
    status = k8s_cluster.status()
    etcd_unit = unit_names(k8s_cluster, "etcd")[0]
    address = status.get_units("etcd")[etcd_unit].public_address
    expected = f"https://{address}:{unit_port(status, 'etcd', etcd_unit)}"

    k8s_status = _k8s_status(k8s_cluster)
    assert k8s_status["ready"], "Cluster isn't ready"
    assert k8s_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert expected in k8s_status["datastore"]["servers"]


def test_update_etcd_cluster(k8s_cluster: jubilant.Juju, timeout: int):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    count = 3 - len(k8s_cluster.status().get_units("etcd"))
    if count > 0:
        k8s_cluster.add_unit("etcd", num_units=count)
    wait_active(k8s_cluster, timeout=max(60, timeout) * 60)

    expected_servers = _etcd_servers(k8s_cluster.status())

    k8s_status = _k8s_status(k8s_cluster)
    assert k8s_status["ready"], "Cluster isn't ready"
    assert k8s_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(k8s_status["datastore"]["servers"]) == expected_servers
