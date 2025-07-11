#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Dualstack tests."""

import datetime
import ipaddress
import logging
from pathlib import Path

import juju.model
import pytest
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.utils import create_from_yaml

from .helpers import get_leader, ready_nodes, wait_pod_phase

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test_dualstack/bundle.yaml", apps_local=["k8s"]),
    pytest.mark.clouds(
        "lxd",
        profiles=["test_dualstack/dualstack.profile"],
        networks=["test_dualstack/dualstack.network"],
    ),
]


async def test_nodes_ready(kubernetes_cluster: juju.model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    expected_nodes = len(k8s.units)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_kube_system_pods(kubernetes_cluster: juju.model.Model):
    """Test that the kube-system pods are running."""
    k8s = kubernetes_cluster.applications["k8s"]
    leader_idx = await get_leader(k8s)
    leader = k8s.units[leader_idx]
    await wait_pod_phase(leader, None, "Running", namespace="kube-system")


def wait_for_nginx_service(api_client: ApiClient, name: str, namespace: str):
    """Wait for the nginx service to be ready."""
    v1 = CoreV1Api(api_client)
    now = datetime.datetime.now()
    timeout_15s = now + datetime.timedelta(seconds=15)
    while (now := datetime.datetime.now()) < timeout_15s:
        svc = v1.read_namespaced_service(name, namespace)
        if svc.spec.cluster_i_ps:
            return svc
    pytest.fail("Service did not become ready in time")


@pytest.fixture()
def deploy_dualstack(api_client: ApiClient):
    """Create services from the dualstack nginx yaml."""
    nginx_dualstack = Path("tests/integration/data/test_dualstack/nginx-dualstack.yaml")
    deployment, services = create_from_yaml(api_client, nginx_dualstack)
    yield deployment, services
    v1svc, v1app = CoreV1Api(api_client), AppsV1Api(api_client)
    for svc in services:
        v1svc.delete_namespaced_service(svc.metadata.name, svc.metadata.namespace)
    for deploy in deployment:
        v1app.delete_namespaced_deployment(deploy.metadata.name, deploy.metadata.namespace)


async def test_nginx_dualstack(deploy_dualstack, api_client: ApiClient):
    """Test that dualstack is enabled."""
    _, services = deploy_dualstack
    assert services, "No services created from dualstack nginx yaml"
    name, namespace = services[0].metadata.name, services[0].metadata.namespace
    svc = wait_for_nginx_service(api_client, name, namespace)
    assert svc.spec.cluster_i_ps, "Service does not have cluster IPs"
    as_obj = [ipaddress.ip_address(ip) for ip in svc.spec.cluster_i_ps]
    assert any(ip.version == 4 for ip in as_obj), "IPv4 not found in cluster IPs"
    assert any(ip.version == 6 for ip in as_obj), "IPv6 not found in cluster IPs"
