#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import pytest
from juju import model
from kubernetes.client import ApiClient

from . import storage

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
pytestmark = [pytest.mark.bundle(file="test-bundle-ceph.yaml", apps_local=["k8s"])]


@pytest.mark.abort_on_fail
async def test_ceph_sc(kubernetes_cluster: model.Model, api_client: ApiClient):
    """Test that a ceph storage class is available and validate pv attachments."""
    manifests = storage.StorageProviderManifests(
        "ceph-xfs-pvc.yaml", "pv-writer-pod.yaml", "pv-reader-pod.yaml"
    )
    definition = storage.StorageProviderTestDefinition(
        "ceph", "ceph-xfs", "rbd.csi.ceph.com", kubernetes_cluster, manifests
    )
    await storage.exec_storage_class(definition, api_client)
