# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""


import contextlib
from unittest import mock

import ops
import ops.testing
import pytest
from charm import K8sCharm


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    harness = ops.testing.Harness(K8sCharm)
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


@contextlib.contextmanager
def mock_reconciler_handlers(harness):
    """Mock out reconciler handlers.

    Args:
        harness: the harness under test

    Yields:
        Mapping of handler_names to their mock methods.
    """
    handler_names = {
        "_evaluate_removal",
        "_install_k8s_snap",
        "_apply_snap_requirements",
        "_check_k8sd_ready",
        "_join_cluster",
        "_configure_cos_integration",
        "_update_status",
        "_apply_node_labels",
    }
    if harness.charm.is_control_plane:
        handler_names |= {
            "_bootstrap_k8s_snap",
            "_enable_functionalities",
            "_create_cluster_tokens",
            "_create_cos_tokens",
            "_apply_cos_requirements",
            "_copy_internal_kubeconfig",
            "_revoke_cluster_tokens",
            "_ensure_cluster_config",
            "_expose_ports",
        }

    handlers = [mock.patch(f"charm.K8sCharm.{name}") for name in handler_names]
    yield dict(zip(handler_names, (h.start() for h in handlers)))
    for handler in handlers:
        handler.stop()


def test_config_changed_invalid(harness):
    """Trigger a config-changed event with an unknown-config option.

    Args:
        harness: the harness under test
    """
    with pytest.raises(ValueError):
        harness.update_config({"unknown-config": "foobar"})


def test_update_status(harness):
    """Test emitting the update_status hook while reconciled.

    Args:
        harness: the harness under test
    """
    harness.charm.reconciler.stored.reconciled = True  # Pretended to be reconciled
    harness.charm.on.update_status.emit()
    assert harness.model.unit.status == ops.WaitingStatus("Cluster not yet ready")


def test_set_leader(harness):
    """Test emitting the set_leader hook while not reconciled.

    Args:
        harness: the harness under test
    """
    harness.charm.reconciler.stored.reconciled = False  # Pretended to not be reconciled
    with mock_reconciler_handlers(harness) as handlers:
        harness.set_leader(True)
    assert harness.model.unit.status == ops.ActiveStatus("Ready")
    assert harness.charm.reconciler.stored.reconciled
    called = {name: h for name, h in handlers.items() if h.called}
    assert len(called) == len(handlers)
