# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Openstack specific Integration tests."""

import juju.model
import pytest

from .helpers import get_leader, wait_pod_phase

pytestmark = [
    pytest.mark.bundle(file="test-bundle-openstack.yaml", apps_local=["k8s", "k8s-worker"]),
    pytest.mark.clouds("openstack"),
]


async def test_o7k_pods(kubernetes_cluster: juju.model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    await wait_pod_phase(leader, None, "Running", namespace="kube-system")
