#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging

import pytest
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, kubernetes_cluster):
    """Deploy the charm and wait for active/idle status."""
    async with ops_test.fast_forward():
        await kubernetes_cluster.wait_for_idle(
            status="active", raise_on_blocked=True, timeout=1000
        )
