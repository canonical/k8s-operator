#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""


import pytest
from juju import model

from .helpers import ready_nodes

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with an external certificates authority, for all the test within this module.
# The charm used for testing is the self-signed-certificates charm.
pytestmark = [
    pytest.mark.bundle_file("test-bundle-certificates.yaml"),
]


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    expected_nodes = len(k8s.units)
    await ready_nodes(k8s.units[0], expected_nodes)
