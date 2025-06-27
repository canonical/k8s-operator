# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests token_distributor module."""

from pathlib import Path

import ops
import ops.testing
import pytest
import token_distributor
from charm import K8sCharm
from literals import CLUSTER_RELATION


def test_request(harness):
    """Test request adds node-name."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation("cluster", "remote")
    collector.request(harness.charm.model.get_relation(CLUSTER_RELATION))
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert data["node-name"] == "my-node"


def test_cluster_name_not_joined(harness):
    """Test cluster name while not bootstrapped."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation("cluster", "remote")
    remote = collector.cluster_name(harness.charm.model.get_relation(CLUSTER_RELATION), False)
    local = collector.cluster_name(harness.charm.model.get_relation(CLUSTER_RELATION), True)
    assert remote == local == ""
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert not data.get("joined")


def test_cluster_name_joined(harness):
    """Test cluster name while not bootstrapped."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation("cluster", "k8s", unit_data={"cluster-name": "my-cluster"})
    # Fetching the remote doesn't update joined field
    remote = collector.cluster_name(harness.charm.model.get_relation(CLUSTER_RELATION), False)
    assert remote == "my-cluster"
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert not data.get("joined")

    # Fetching the local does update joined field
    local = collector.cluster_name(harness.charm.model.get_relation(CLUSTER_RELATION), True)
    assert remote == local == "my-cluster"
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert data["joined"] == "my-cluster"
