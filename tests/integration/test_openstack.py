# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Openstack specific Integration tests."""

from typing import Dict

import jubilant
import pytest
import storage
import yaml
from helpers import fast_forward
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.client.models import V1DaemonSet, V1DaemonSetList, V1NodeList
from literals import ONE_MIN

CLOUD_TYPE = "openstack"
CONTROLLER_NAME = "openstack-cloud-controller-manager"
STORAGE_CLASS_NAME = "csi-cinder-default"

pytestmark = [
    pytest.mark.bundle(file="test-bundle-openstack.yaml", apps_local=["k8s"]),
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


def test_cinder_pv(kubernetes_cluster: jubilant.Juju, api_client: ApiClient):
    """Test that a cinder storage class is available and validate pv attachments."""
    definition = storage.StorageProviderTestDefinition(
        "cinder", STORAGE_CLASS_NAME, "cinder.csi.openstack.org", kubernetes_cluster
    )
    storage.exec_storage_class(definition, api_client)


def test_k8s_api_load_balancer(kubernetes_cluster: jubilant.Juju, api_client: ApiClient):
    """Test k8s api load balancer.

    This test checks that the Kubernetes API server is accessible via a load balancer managed
    by the OpenStack through the openstack-integrator.
    """
    k8s_units = kubernetes_cluster.status().get_units("k8s")
    leader = next(iter(k8s_units))

    # Get the server endpoint
    task = kubernetes_cluster.run(leader, "get-kubeconfig")
    kubeconfig_raw = task.results["kubeconfig"]
    kubeconfig_yaml = yaml.safe_load(kubeconfig_raw)
    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]

    server_endpoint = server_endpoint.removeprefix("https://")
    server_endpoint = server_endpoint[: server_endpoint.rfind(":")]
    server_endpoint = server_endpoint.strip("[")
    server_endpoint = server_endpoint.strip("]")

    for unit in k8s_units.values():
        assert unit.public_address != server_endpoint, "External lb not configured"

    api_client.configuration.host = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    v1 = CoreV1Api(api_client)
    # Just to make sure the connection is working
    node_list: V1NodeList = v1.list_node()
    assert node_list.items, "No nodes found"


def test_extra_sans(kubernetes_cluster: jubilant.Juju, timeout: int):
    """Test extra sans config."""
    extra_san = "test.example.com"
    sans_config = {"kube-apiserver-extra-sans": extra_san}
    with fast_forward(kubernetes_cluster, ONE_MIN):
        kubernetes_cluster.config("k8s", sans_config)
        kubernetes_cluster.wait(jubilant.all_active, timeout=timeout * 60)

    leader = next(iter(kubernetes_cluster.status().get_units("k8s")))

    # Get the server endpoint
    task = kubernetes_cluster.run(leader, "get-kubeconfig")
    kubeconfig_yaml = yaml.safe_load(task.results["kubeconfig"])

    server_endpoint: str = kubeconfig_yaml["clusters"][0]["cluster"]["server"]
    server_endpoint = server_endpoint.removeprefix("https://")

    # The openssl pipeline may exit non-zero; we only care about its stdout.
    cmd = (
        f"echo | openssl s_client -connect {server_endpoint} -servername {extra_san} | "
        f"openssl x509 -noout -text"
    )
    try:
        out = kubernetes_cluster.exec(cmd, unit=leader).stdout
    except jubilant.TaskError as e:
        out = e.task.stdout
    assert extra_san in out, f"Extra SAN {extra_san} not found in certificate"
