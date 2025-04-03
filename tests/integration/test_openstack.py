# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Openstack specific Integration tests."""

import asyncio
from typing import Dict

import juju.model
import pytest
import yaml
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
    definition = storage.StorageProviderTestDefinition(
        "cinder", STORAGE_CLASS_NAME, "cinder.csi.openstack.org", kubernetes_cluster
    )
    await storage.exec_storage_class(definition, api_client)


async def test_external_load_balancer(kubernetes_cluster: juju.model.Model, api_client: ApiClient):
    """Test external load balancer."""
    k8s = kubernetes_cluster.applications["k8s"]

    # Get the server endpoint
    action = await k8s.units[0].run_action("get-kubeconfig")
    result = await action.wait()
    completed = result.status == "completed" or result.results["return-code"] == 0
    assert completed, "Failed to get kubeconfig"
    kubeconfig_raw = result.results["kubeconfig"]
    kubeconfig_yaml = yaml.safe_load(kubeconfig_raw)
    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]

    server_endpoint = server_endpoint.removeprefix("https://")
    server_endpoint = server_endpoint[: server_endpoint.rfind(":")]
    server_endpoint = server_endpoint.strip("[")
    server_endpoint = server_endpoint.strip("]")

    for unit in k8s.units:
        assert unit.get_public_address() != server_endpoint, "External lb not configured"

    api_client.configuration.host = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    v1 = CoreV1Api(api_client)
    # Just to make sure the connection is working
    node_list: V1NodeList = v1.list_node()
    assert node_list.items, "No nodes found"


async def test_extra_sans(kubernetes_cluster: juju.model.Model):
    """Test extra sans config."""
    k8s = kubernetes_cluster.applications["k8s"]

    extra_san = "test.example.com"
    sans_config = {"kube-apiserver-extra-sans": extra_san}
    await asyncio.gather(k8s.set_config(sans_config))
    await kubernetes_cluster.wait_for_idle(status="active", timeout=5 * 60)

    # Get the server endpoint
    action = await k8s.units[0].run_action("get-kubeconfig")
    result = await action.wait()
    completed = result.status == "completed" or result.results["return-code"] == 0
    assert completed, "Failed to get kubeconfig"
    kubeconfig_raw = result.results["kubeconfig"]
    kubeconfig_yaml = yaml.safe_load(kubeconfig_raw)

    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    server_endpoint = server_endpoint.removeprefix("https://")

    result = await k8s.units[0].run(
        f"echo | openssl s_client -connect {server_endpoint} -servername {extra_san} | "
        f"openssl x509 -noout -text"
    )
    result = await result.wait()
    out = result.results["stdout"]
    assert extra_san in out, f"Extra SAN {extra_san} not found in certificate"
