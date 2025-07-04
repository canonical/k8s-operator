#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import pytest
from juju import model

from .helpers import ready_nodes

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with managed etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-managed-etcd.yaml", apps_local=["k8s", "k8s-worker"])
]


# TODO: complete the test
@pytest.mark.skip(reason="skip until the backing k8s snap package has the managed-etcd datastore")
@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)
