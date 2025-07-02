# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests for update_status."""

import unittest.mock as mock

import k8s.node
import ops
import pytest
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
def test_feature_failures(mock_status, charm, upgrader, worker, expected_status):
    """Test the update_status function."""
    handler = Handler(charm, upgrader)
    charm.is_worker = worker

    cluster_status = charm.api_manager.get_cluster_status.return_value
    cluster_status.metadata.status.feature_statuses = [
        ("test-feature", mock.MagicMock(enabled=False, message="Failed to do something here.")),
        ("working-feature", mock.MagicMock(enabled=True, message="Working fine.")),
    ]
    handler.run()
    mock_status.add.assert_called_once_with(expected_status)


@mock.patch(
    "events.update_status.ready", new=mock.MagicMock(return_value=k8s.node.Status.NOT_READY)
)
@mock.patch("reschedule.PeriodicEvent", new=mock.MagicMock())
@mock.patch("events.update_status.status")
def test_cant_get_features(mock_status, charm, upgrader):
    """Test the update_status function when features cannot be retrieved."""
    handler = Handler(charm, upgrader)
    charm.is_worker = False

    charm.api_manager.get_cluster_status.side_effect = K8sdAPIManagerError("API error")
    handler.run()
    mock_status.add.assert_called_once_with(ops.WaitingStatus("Waiting to verify features"))
