# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests token_distributor module."""

import json
import unittest.mock as mock

import ops
import pytest
import token_distributor
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


@pytest.mark.parametrize("revision", [1, "2"])
@pytest.mark.parametrize("token", ["my-token"])
def test_token_content(revision, token):
    """Test token content."""
    as_dict = {"revision": revision, "token": token}
    as_json = json.dumps(as_dict)
    secret = mock.MagicMock(spec=ops.Secret)
    secret.get_content.return_value = as_dict

    content = [
        token_distributor.TokenContent(revision=revision, token=token),
        token_distributor.TokenContent.model_validate(as_dict),
        token_distributor.TokenContent.model_validate_json(as_json),
        token_distributor.TokenContent.load_from_secret(secret),
    ]

    int_rev = int(revision)
    str_rev = str(int_rev)
    for c in content:
        assert c.revision == int_rev
        assert c.token.get_secret_value() == "my-token"
        assert c.model_dump() == {"revision": str_rev, "token": "my-token"}
        assert c.model_dump_json() == f'{{"revision":"{str_rev}","token":"my-token"}}'
