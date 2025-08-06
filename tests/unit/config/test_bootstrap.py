# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

import config.bootstrap
import pytest

import charms.contextual_status


def test_node_taints_valid(harness):
    """Test valid node taints configuration.

    Args:
        harness: the harness under test
    """
    harness.disable_hooks()
    harness.update_config(
        {"bootstrap-node-taints": "   newTaint1:NoSchedule   newTaint2:PreferNoSchedule   "}
    )
    bootstrap_node_taints = config.bootstrap.node_taints(harness.charm)
    assert bootstrap_node_taints == ["newTaint1:NoSchedule", "newTaint2:PreferNoSchedule"]


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
