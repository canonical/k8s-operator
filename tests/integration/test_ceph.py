#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import jubilant
import pytest
import storage
from kubernetes.client import ApiClient

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test-bundle-ceph.yaml", apps_local=APPS),
    pytest.mark.architecture("amd64"),
]


def test_ceph_sc(k8s_cluster: jubilant.Juju, api_client: ApiClient):
    """Test that a ceph storage class is available and validate pv attachments."""
    manifests = storage.StorageProviderManifests(
        "ceph-xfs-pvc.yaml", "pv-writer-pod.yaml", "pv-reader-pod.yaml"
    )
    definition = storage.StorageProviderTestDefinition(
        "ceph", "ceph-xfs", "rbd.csi.ceph.com", k8s_cluster, manifests
    )
    storage.exec_storage_class(definition, api_client)
