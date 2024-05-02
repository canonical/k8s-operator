# Copyright 2024 Canonical Ltd.
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


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    meta = Path(__file__).parent / "../../charmcraft.yaml"
    if request.param == "worker":
        meta = Path(__file__).parent / "../../../charmcraft.yaml"
    harness = ops.testing.Harness(K8sCharm, meta=meta.read_text())
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


def test_request(harness):
    """Test request adds node-name."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation("cluster", "remote")
    collector.request(harness.charm.model.get_relation("cluster"))
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert data["node-name"] == "my-node"


def test_cluster_name_not_joined(harness):
    """Test cluster name while not bootstrapped."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation("cluster", "remote")
    remote = collector.cluster_name(harness.charm.model.get_relation("cluster"), False)
    local = collector.cluster_name(harness.charm.model.get_relation("cluster"), True)
    assert remote == local == ""
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert not data.get("joined")


def test_cluster_name_joined(harness):
    """Test cluster name while not bootstrapped."""
    harness.disable_hooks()
    collector = token_distributor.TokenCollector(harness.charm, "my-node")
    relation_id = harness.add_relation(
        "cluster", "remote", unit_data={"cluster-name": "my-cluster"}
    )
    # Fetching the remote doesn't update joined field
    remote = collector.cluster_name(harness.charm.model.get_relation("cluster"), False)
    assert remote == "my-cluster"
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert not data.get("joined")

    # Fetching the local does update joined field
    local = collector.cluster_name(harness.charm.model.get_relation("cluster"), True)
    assert remote == local == "my-cluster"
    data = harness.get_relation_data(relation_id, harness.charm.unit.name)
    assert data["joined"] == "my-cluster"
