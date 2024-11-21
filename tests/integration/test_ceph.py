#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

from pathlib import Path

import pytest
from juju import model, unit

from . import helpers

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
pytestmark = [
    pytest.mark.bundle_file("test-bundle-ceph.yaml"),
    pytest.mark.ignore_blocked,
]


def _get_data_file_path(name) -> str:
    """Retrieve the full path of the specified test data file."""
    path = Path(__file__).parent / "data" / "test_ceph" / name
    return str(path)


@pytest.mark.abort_on_fail
async def test_ceph_sc(kubernetes_cluster: model.Model):
    """Test that a ceph storage class is available and validate pv attachments."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    event = await k8s.run("k8s kubectl get sc -o=jsonpath='{.items[*].provisioner}'")
    result = await event.wait()
    stdout = result.results["stdout"]
    assert "rbd.csi.ceph.com" in stdout, f"No ceph provisioner found in: {stdout}"

    # Copy pod definitions.
    definitions = ["ceph-xfs-pvc.yaml", "pv-writer-pod.yaml", "pv-reader-pod.yaml"]
    for fname in definitions:
        await k8s.scp_to(_get_data_file_path(fname), f"/tmp/{fname}")

    try:
        # Create "ceph-xfs" PVC.
        event = await k8s.run("k8s kubectl apply -f /tmp/ceph-xfs-pvc.yaml")
        result = await event.wait()
        assert result.results["return-code"] == 0, "Failed to create pvc."

        # Create a pod that writes to the Ceph PV.
        event = await k8s.run("k8s kubectl apply -f /tmp/pv-writer-pod.yaml")
        result = await event.wait()
        assert result.results["return-code"] == 0, "Failed to create writer pod."

        # Wait for the pod to exit successfully.
        await helpers.wait_pod_phase(k8s, "pv-writer-test", "Succeeded")

        # Create a pod that reads the PV data and writes it to the log.
        event = await k8s.run("k8s kubectl apply -f /tmp/pv-reader-pod.yaml")
        result = await event.wait()
        assert result.results["return-code"] == 0, "Failed to create reader pod."

        await helpers.wait_pod_phase(k8s, "pv-reader-test", "Succeeded")

        # Check the logged PV data.
        logs = await helpers.get_pod_logs(k8s, "pv-reader-test")
        assert "PVC test data" in logs
    finally:
        # Cleanup
        for fname in definitions:
            await k8s.run(f"k8s kubectl delete -f /tmp/{fname}")
