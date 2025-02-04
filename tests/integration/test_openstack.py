# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Openstack specific Integration tests."""

from typing import Dict

import juju.model
import pytest
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.client.models import V1DaemonSet, V1DaemonSetList, V1NodeList

from . import storage

CLOUD_TYPE = "openstack"
CONTROLLER_NAME = "openstack-cloud-controller-manager"
STORAGE_CLASS_NAME = "csi-cinder-default"

pytestmark = [
    pytest.mark.bundle(file="test-bundle-openstack.yaml", apps_local=["k8s"]),
    pytest.mark.clouds(CLOUD_TYPE),
]


async def test_cloud_provider(api_client: ApiClient):
    """Verify the cloud controller is running."""
    v1 = AppsV1Api(api_client)
    ds_list: V1DaemonSetList = v1.list_namespaced_daemon_set(namespace="kube-system")
    assert ds_list.items, "No DaemonSets found"
    by_name: Dict[str, V1DaemonSet] = {ds.metadata.name: ds for ds in ds_list.items}
    assert CONTROLLER_NAME in by_name, f"DaemonSet {CONTROLLER_NAME} not found"
    ds = by_name[CONTROLLER_NAME]
    assert ds.status, f"No status found for {CONTROLLER_NAME}"
    assert ds.status.number_ready == ds.status.desired_number_scheduled, "Controller not ready"


async def test_provider_ids(api_client: ApiClient):
    """Verify the cloud controller has assigned provider ids."""
    v1 = CoreV1Api(api_client)
    node_list: V1NodeList = v1.list_node()
    assert node_list.items, "No nodes found"
    for node in node_list.items:
        assert node.spec.provider_id, f"No provider-id found on {node.metadata.name}"
        assert node.spec.provider_id.startswith(f"{CLOUD_TYPE}://")


async def test_cinder_pv(kubernetes_cluster: juju.model.Model, api_client: ApiClient):
    """Test that a cinder storage class is available and validate pv attachments."""
    manifests = storage.StorageProviderManifests(
        "cinder-pvc.yaml", "pv-writer-pod.yaml", "pv-reader-pod.yaml"
    )
    definition = storage.StorageProviderTestDefinition(
        "cinder", STORAGE_CLASS_NAME, "cinder.csi.openstack.org", kubernetes_cluster, manifests
    )
    await storage.exec_storage_class(definition, api_client)
