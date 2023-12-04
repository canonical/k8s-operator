# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""


import ops
import ops.testing
import pytest
from charm import K8sCharm


@pytest.fixture()
def harness():
    harness = ops.testing.Harness(K8sCharm)
    harness.begin()
    yield harness
    harness.cleanup()


def test_config_changed_invalid(harness):
    # Trigger a config-changed event with an updated value
    with pytest.raises(ValueError):
        harness.update_config({"log-level": "foobar"})
