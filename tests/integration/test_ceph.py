#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import re

import pytest
import pytest_asyncio
import storage
from juju import model
from kubernetes.client import ApiClient, CoreV1Api, StorageV1Api
from kubernetes.client import models as k8s_models
from pytest_operator.plugin import OpsTest

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-ceph.yaml", apps_local=["k8s"]),
    pytest.mark.architecture("amd64"),
]
CEPH_CSI_MISSING_NS = re.compile(r"Missing namespace '(\S+)'")


@pytest_asyncio.fixture(scope="module")
async def ready_csi_apps(
    ops_test: OpsTest, kubernetes_cluster: model.Model, api_client: ApiClient
) -> None:
    """Wait for the CSI apps to be ready."""
    v1 = CoreV1Api(api_client)
    csi_apps = [
        app for app in kubernetes_cluster.applications.values() if app.charm_name == "ceph-csi"
    ]
    for app in csi_apps:
        for unit in app.units:
            if m := CEPH_CSI_MISSING_NS.match(unit.workload_status_message):
                namespace = m.group(1)
                v1.create_namespace(
                    body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
                )
                break

    async with ops_test.fast_forward():
        await kubernetes_cluster.wait_for_idle(
            apps=[app.name for app in csi_apps], status="active", timeout=60 * 5
        )


@pytest.mark.usefixtures("ready_csi_apps")
async def test_ceph_sc(kubernetes_cluster: model.Model, api_client: ApiClient):
    """Test that a ceph storage class is available and validate pv attachments."""
    v1 = StorageV1Api(api_client)
    classes = v1.list_storage_class()
    ceph_classes = [sc for sc in classes.items if "ceph" in sc.metadata.name]
    assert len(ceph_classes) > 0, "No ceph storage classes found"
    for sc in ceph_classes:
        definition = storage.StorageProviderTestDefinition(
            "ceph", sc.metadata.name, sc.provisioner, kubernetes_cluster
        )
        await storage.exec_storage_class(definition, api_client)
