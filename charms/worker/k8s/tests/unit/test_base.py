# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""


import ops
import ops.testing
import pytest

from charm import K8sCharm


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    harness = ops.testing.Harness(K8sCharm)
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


def test_config_changed_invalid(harness):
    # Trigger a config-changed event with an unknown-config option
    with pytest.raises(ValueError):
        harness.update_config({"unknown-config": "foobar"})


def test_update_status(harness):
    harness.charm.reconciler.stored.reconciled = True  # Pretended to be reconciled
    harness.charm.on.update_status.emit()
    if harness.charm.is_control_plane:
        assert harness.model.unit.status == ops.WaitingStatus("Cluster not yet ready")
    else:
        assert harness.model.unit.status == ops.ActiveStatus("Ready")
