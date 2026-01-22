# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests token_distributor module."""

import json
import unittest.mock as mock
from collections import defaultdict

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


@pytest.mark.parametrize(
    "manager_klass",
    [
        token_distributor.ClusterTokenManager,
        token_distributor.CosTokenManager,
    ],
)
def test_token_manager_grant(manager_klass, request, caplog):
    """Test token manager can share into correct relation databag."""
    api_manager_mock = mock.MagicMock(spec=token_distributor.K8sdAPIManager)
    manager = manager_klass(api_manager_mock())
    charm = mock.MagicMock(spec=ops.CharmBase)()
    unit = mock.MagicMock(spec=ops.Unit)()
    secret = mock.MagicMock(spec=ops.Secret)()
    relation = mock.MagicMock()
    relation.name = request.node.name
    relation.data = defaultdict(dict)
    secret_key = token_distributor.CLUSTER_SECRET_ID.format(unit.name)
    relation.data[charm.unit][secret_key] = "my-value"

    caplog.set_level("DEBUG")

    manager.grant(relation, charm, unit, secret)
    assert relation.data[charm.app][secret_key] == secret.id
    assert secret_key not in relation.data[charm.unit]

    title = manager_klass.strategy.name.title()
    assert f"Grant {title} token for '{secret_key}' on {relation.name}" in caplog.text


@pytest.mark.parametrize(
    "manager_klass",
    [
        token_distributor.ClusterTokenManager,
        token_distributor.CosTokenManager,
    ],
)
def test_token_manager_get_revoke(manager_klass, request, caplog):
    """Test token manager can unshare from correct relation databag."""
    api_manager_mock = mock.MagicMock(spec=token_distributor.K8sdAPIManager)
    manager = manager_klass(api_manager_mock())
    charm = mock.MagicMock(spec=ops.CharmBase)()
    unit = mock.MagicMock(spec=ops.Unit)()
    relation = mock.MagicMock()
    relation.name = request.node.name
    relation.data = defaultdict(dict)
    secret_key = token_distributor.CLUSTER_SECRET_ID.format(unit.name)
    relation.data[charm.app][secret_key] = "my-value"
    relation.data[charm.unit][secret_key] = "my-value"
    caplog.set_level("DEBUG")

    secret = manager.get_juju_secret(relation, charm, unit)
    charm.model.get_secret.assert_called_once_with(id="my-value")

    manager.revoke(relation, charm, unit)
    assert secret_key not in relation.data[charm.unit]
    assert secret_key not in relation.data[charm.app]
    secret.remove_all_revisions.assert_called_once_with()

    title = manager_klass.strategy.name.title()
    assert f"Found {title} token for '{secret_key}' on {relation.name}" in caplog.text
    assert f"Revoke {title} token for '{secret_key}' on {relation.name}" in caplog.text
