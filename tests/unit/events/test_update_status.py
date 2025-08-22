# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests for update_status."""

import unittest.mock as mock

import k8s.node
import ops
import pytest
from config.bootstrap import Controller as BootstrapController
from events.update_status import Handler
from upgrade import K8sUpgrade

from charms.k8s.v0.k8sd_api_manager import K8sdAPIManagerError


@pytest.fixture
def charm():
    """Fixture for K8sCharmProtocol."""
    charm = mock.MagicMock()
    charm.framework = charm
    charm.get_cluster_name.return_value = "test-cluster"
    charm.get_node_name.return_value = "test-node"
    return charm


@pytest.fixture
def upgrader() -> K8sUpgrade:
    """Fixture for K8sUpgrade."""
    return mock.MagicMock(spec=K8sUpgrade)


@pytest.fixture
def bootstrap() -> BootstrapController:
    """Fixture for BootstrapController."""
    return mock.MagicMock(spec=BootstrapController)


@pytest.mark.parametrize(
    "worker, expected_status",
    [
        (True, ops.WaitingStatus("Node test-node not ready")),
        (False, ops.BlockedStatus("Feature 'test-feature' is not ready")),
    ],
)
@mock.patch(
    "events.update_status.ready", new=mock.MagicMock(return_value=k8s.node.Status.NOT_READY)
)
@mock.patch("reschedule.PeriodicEvent", new=mock.MagicMock())
@mock.patch("events.update_status.status")
def test_feature_failures(mock_status, charm, upgrader, bootstrap, worker, expected_status):
    """Test the update_status function."""
    handler = Handler(charm, bootstrap, upgrader)
    charm.is_worker = worker

    cluster_status = charm.api_manager.get_cluster_status.return_value
    cluster_status.metadata.status.by_feature = [
        (
            "test-feature",
            mock.MagicMock(enabled=True),
            mock.MagicMock(enabled=False, message="Failed to do something here."),
        ),
        (
            "working-feature",
            mock.MagicMock(enabled=True),
            mock.MagicMock(enabled=False, message="Working fine."),
        ),
        (
            "disabled-feature",
            mock.MagicMock(enabled=False),
            mock.MagicMock(enabled=False, message="Not deployed."),
        ),
    ]
    handler.run()
    mock_status.add.assert_called_once_with(expected_status)


@mock.patch(
    "events.update_status.ready", new=mock.MagicMock(return_value=k8s.node.Status.NOT_READY)
)
@mock.patch("reschedule.PeriodicEvent", new=mock.MagicMock())
@mock.patch("events.update_status.status")
def test_cant_get_features(mock_status, charm, bootstrap, upgrader):
    """Test the update_status function when features cannot be retrieved."""
    handler = Handler(charm, bootstrap, upgrader)
    charm.is_worker = False

    charm.api_manager.get_cluster_status.side_effect = K8sdAPIManagerError("API error")
    handler.run()
    mock_status.add.assert_called_once_with(ops.WaitingStatus("Waiting to verify features"))


@mock.patch("events.update_status.ready", new=mock.MagicMock(return_value=k8s.node.Status.READY))
@mock.patch("reschedule.PeriodicEvent", new=mock.MagicMock())
def test_bootstrap_prevent(bootstrap, charm, upgrader):
    """Test that bootstrap prevent method is called."""
    bootstrap.prevent.return_value = ops.BlockedStatus("Bootstrap config is immutable")
    handler = Handler(charm, bootstrap, upgrader)
    handler.run()
    bootstrap.prevent.assert_called_once()
