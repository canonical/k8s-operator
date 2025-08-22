# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest.mock as mock

import config.bootstrap
import pytest

import charms.contextual_status
import charms.k8s.v0.k8sd_api_manager as k8sd


@pytest.mark.parametrize(
    "taints,expected",
    [
        ("", []),
        ("key1=:NoSchedule", ["key1=:NoSchedule"]),
        ("key1=value1:NoSchedule", ["key1=value1:NoSchedule"]),
        (
            "key1=value1:NoSchedule key2=value2:NoSchedule",
            ["key1=value1:NoSchedule", "key2=value2:NoSchedule"],
        ),
    ],
)
def test_node_taints_valid(harness, taints, expected):
    """Test valid node taints configuration.

    Args:
        harness: the harness under test
        taints: the taints to test
        expected: the expected result after processing the taints
    """
    harness.disable_hooks()
    harness.update_config({"bootstrap-node-taints": taints})
    bootstrap_node_taints = config.bootstrap.node_taints(harness.charm)
    assert bootstrap_node_taints == expected


@pytest.mark.parametrize(
    "taints",
    [
        "key1",
        "key1=value1",
        "key1=value1:NoSchedule,",
        "key1==value1:NoSchedule",
        "key1=@invalid:NoSchedule",
        "key1=invalid:@NoSchedule",
    ],
)
def test_node_taints_invalid(harness, taints):
    """Test valid node taints configuration.

    Args:
        harness: the harness under test
        taints: the invalid taints to test
    """
    harness.disable_hooks()
    harness.update_config({"bootstrap-node-taints": taints})
    with pytest.raises(charms.contextual_status.ReconcilerError):
        config.bootstrap.node_taints(harness.charm)


def test_load_certificates_provider_not_bootstrapped(harness):
    """Test loading the certificates provider from the cluster relation."""
    # Test with no relation
    harness.disable_hooks()
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == ""

    # test with a relation that has no data
    harness.add_relation(config.bootstrap.CLUSTER_RELATION, harness.charm.app.name)
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == ""

    # Test with a relation that has data
    config.bootstrap._persist_certificates_provider(harness.charm, "TRUSTED")
    config.bootstrap._persist_certificates_provider(harness.charm, "UNCHANGED")
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == "TRUSTED"


def test_load_certificates_provider_bootstrapped(harness):
    """Test loading the certificates provider from the cluster relation when bootstrapped."""
    harness.charm.api_manager.get_node_status = mock.MagicMock()
    undefined_provider = ""

    # Test with no relation
    harness.disable_hooks()
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == undefined_provider

    # test with a relation that has no data
    harness.add_relation(config.bootstrap.CLUSTER_RELATION, harness.charm.app.name)
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == undefined_provider

    # Test with a relation that has data
    config.bootstrap._persist_certificates_provider(harness.charm, "TRUSTED")
    # Make sure the first persisted value is retained
    config.bootstrap._persist_certificates_provider(harness.charm, "UNCHANGED")
    provider = config.bootstrap._load_certificates_provider(harness.charm)
    assert provider == "TRUSTED"


def test_load_immutable(harness):
    """Test loading the immutable bootstrap configuration options."""
    harness.add_relation(config.bootstrap.CLUSTER_RELATION, harness.charm.app.name)
    config.bootstrap._persist_certificates_provider(harness.charm, "TRUSTED")
    if harness.charm.is_control_plane:
        cc = harness.charm.api_manager.get_cluster_config = mock.MagicMock()
        cc.return_value.metadata.datastore.type = "etcd"
        cc.return_value.metadata.pod_cidr = "10.1.0.0/16"
        cc.return_value.metadata.service_cidr = "10.1.2.0/24"

    harness.disable_hooks()
    controller = harness.charm.bootstrap
    immutable = controller.load_immutable()

    if harness.charm.is_control_plane:
        assert immutable.datastore == "managed-etcd"
        assert immutable.pod_cidr == "10.1.0.0/16"
        assert immutable.service_cidr == "10.1.2.0/24"
    else:
        assert immutable.datastore is None
        assert immutable.pod_cidr is None
        assert immutable.service_cidr is None
    assert immutable.certificates == "TRUSTED"


@pytest.mark.xfail(reason="Certificate validation is currently disabled.")
def test_validate_certificates(harness):
    """Test validating the bootstrap-certificates option."""
    harness.disable_hooks()
    harness.update_config({"bootstrap-datastore": ""})
    harness.add_relation(config.bootstrap.CLUSTER_RELATION, harness.charm.app.name)
    config.bootstrap._persist_certificates_provider(harness.charm, "INVALID")
    harness.charm.bootstrap.immutable = harness.charm.bootstrap.load_immutable()

    cc = harness.charm.api_manager.get_cluster_config = mock.MagicMock()
    cc.side_effect = k8sd.K8sdConnectionError("Test error")

    with pytest.raises(config.bootstrap.context_status.ReconcilerError) as ie:
        harness.charm.bootstrap.validate()
    assert "bootstrap-certificates='INVALID' is invalid." in str(ie.value)


def test_validate_datastore(harness):
    """Test validating the bootstrap-datastore option."""
    if harness.charm.is_worker:
        pytest.skip("Datastore validation is only relevant for control plane charms.")
    harness.disable_hooks()
    harness.update_config({"bootstrap-datastore": "INVALID"})
    cc = harness.charm.api_manager.get_cluster_config = mock.MagicMock()
    cc.side_effect = k8sd.K8sdConnectionError("Test error")

    with pytest.raises(config.bootstrap.context_status.ReconcilerError) as ie:
        harness.charm.bootstrap.validate()
    assert "bootstrap-datastore='INVALID' is invalid." in str(ie.value)

    harness.update_config({"bootstrap-datastore": ""})
    harness.charm.bootstrap.validate()


@mock.patch("config.bootstrap._load_certificates_provider", mock.MagicMock(return_value="TRUSTED"))
@mock.patch("config.bootstrap._persist_certificates_provider")
def test_persist(mock_persist, harness):
    """Test persisting the bootstrap configuration options."""
    if harness.charm.is_worker:
        pytest.skip("Persist is only relevant for control plane charms.")

    harness.disable_hooks()
    harness.charm.bootstrap.immutable = harness.charm.bootstrap.load_immutable()
    harness.update_config(
        {
            "bootstrap-datastore": "etcd",
            "bootstrap-pod-cidr": "10.1.0.0/16",
            "bootstrap-service-cidr": "10.1.2.0/24",
        }
    )
    assert harness.charm.bootstrap.immutable.datastore is None
    assert harness.charm.bootstrap.immutable.pod_cidr is None
    assert harness.charm.bootstrap.immutable.service_cidr is None
    assert harness.charm.bootstrap.immutable.certificates == "TRUSTED"
    harness.charm.bootstrap.persist()
    mock_persist.assert_called_once_with(harness.charm, "TRUSTED")
    assert harness.charm.bootstrap.immutable.datastore == "etcd"
    assert harness.charm.bootstrap.immutable.pod_cidr == "10.1.0.0/16"
    assert harness.charm.bootstrap.immutable.service_cidr == "10.1.2.0/24"
    assert harness.charm.bootstrap.immutable.certificates == "TRUSTED"
