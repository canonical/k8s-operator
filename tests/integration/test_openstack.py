# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Openstack specific Integration tests."""

from typing import Dict

import jubilant
import pytest
import storage
import yaml
from helpers import fast_forward, get_leader, unit_names, wait_active
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.client.models import V1DaemonSet, V1DaemonSetList, V1NodeList
from literals import ONE_MIN

CLOUD_TYPE = "openstack"
CONTROLLER_NAME = "openstack-cloud-controller-manager"
STORAGE_CLASS_NAME = "csi-cinder-default"

APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test-bundle-openstack.yaml", apps_local=APPS),
    pytest.mark.clouds(CLOUD_TYPE),
    pytest.mark.architecture("amd64"),
]


def test_cloud_provider(api_client: ApiClient):
    """Verify the cloud controller is running."""
    v1 = AppsV1Api(api_client)
    ds_list: V1DaemonSetList = v1.list_namespaced_daemon_set(namespace="kube-system")
    assert ds_list.items, "No DaemonSets found"
    by_name: Dict[str, V1DaemonSet] = {ds.metadata.name: ds for ds in ds_list.items}
    assert CONTROLLER_NAME in by_name, f"DaemonSet {CONTROLLER_NAME} not found"
    ds = by_name[CONTROLLER_NAME]
    assert ds.status, f"No status found for {CONTROLLER_NAME}"
    assert ds.status.number_ready == ds.status.desired_number_scheduled, "Controller not ready"


def test_provider_ids(api_client: ApiClient):
    """Verify the cloud controller has assigned provider ids."""
    v1 = CoreV1Api(api_client)
    node_list: V1NodeList = v1.list_node()
    assert node_list.items, "No nodes found"
    for node in node_list.items:
        assert node.spec.provider_id, f"No provider-id found on {node.metadata.name}"
        assert node.spec.provider_id.startswith(f"{CLOUD_TYPE}://")


def test_cinder_pv(k8s_cluster: jubilant.Juju, api_client: ApiClient):
    """Test that a cinder storage class is available and validate pv attachments."""
    manifests = storage.StorageProviderManifests(
        "cinder-pvc.yaml", "pv-writer-pod.yaml", "pv-reader-pod.yaml"
    )
    definition = storage.StorageProviderTestDefinition(
        "cinder", STORAGE_CLASS_NAME, "cinder.csi.openstack.org", k8s_cluster, manifests
    )
    storage.exec_storage_class(definition, api_client)


def test_api_load_balancer(k8s_cluster: jubilant.Juju, api_client: ApiClient):
    """Test k8s api load balancer.

    This test checks that the Kubernetes API server is accessible via a load balancer managed
    by the OpenStack through the openstack-integrator.
    """
    unit = unit_names(k8s_cluster, "k8s")[0]
    kubeconfig_yaml = yaml.safe_load(k8s_cluster.run(unit, "get-kubeconfig").results["kubeconfig"])
    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]

    server_endpoint = server_endpoint.removeprefix("https://")
    server_endpoint = server_endpoint[: server_endpoint.rfind(":")]
    server_endpoint = server_endpoint.strip("[")
    server_endpoint = server_endpoint.strip("]")

    for unit_status in k8s_cluster.status().get_units("k8s").values():
        assert unit_status.public_address != server_endpoint, "External lb not configured"

    api_client.configuration.host = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    v1 = CoreV1Api(api_client)
    # Just to make sure the connection is working
    node_list: V1NodeList = v1.list_node()
    assert node_list.items, "No nodes found"


def test_extra_sans(k8s_cluster: jubilant.Juju, timeout: int):
    """Test extra sans config."""
    extra_san = "test.example.com"
    with fast_forward(k8s_cluster, ONE_MIN):
        k8s_cluster.config("k8s", {"kube-apiserver-extra-sans": extra_san})
        wait_active(k8s_cluster, timeout=timeout * 60)

    unit = get_leader(k8s_cluster, "k8s")
    kubeconfig_yaml = yaml.safe_load(k8s_cluster.run(unit, "get-kubeconfig").results["kubeconfig"])
    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    server_endpoint = server_endpoint.removeprefix("https://")

    # openssl s_client can exit non-zero even when it printed the certificate.
    out = k8s_cluster.exec(
        f"echo | openssl s_client -connect {server_endpoint} -servername {extra_san} | "
        f"openssl x509 -noout -text || true",
        unit=unit,
    ).stdout
    assert extra_san in out, f"Extra SAN {extra_san} not found in certificate"
