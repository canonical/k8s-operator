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
from helpers import get_leader, ready_nodes, wait_pod_phase
from kubernetes.client import ApiClient, AppsV1Api, CoreV1Api
from kubernetes.utils import create_from_yaml

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test_ipv6only/bundle.yaml", apps_local=["k8s"]),
    pytest.mark.clouds(
        "lxd",
        profiles=["test_ipv6only/ipv6.profile"],
        networks=["test_ipv6only/ipv6.network"],
    ),
    pytest.mark.skip("Skipping ipv6-only tests"),
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
def deploy_ipv6_only(api_client: ApiClient):
    """Create services from the ipv6-only nginx yaml."""
    nginx_ipv6_only = Path("tests/integration/data/test_ipv6only/nginx-ipv6-only.yaml")
    deployment, services = create_from_yaml(api_client, nginx_ipv6_only)
    yield deployment, services
    v1svc, v1app = CoreV1Api(api_client), AppsV1Api(api_client)
    for svc in services:
        v1svc.delete_namespaced_service(svc.metadata.name, svc.metadata.namespace)
    for deploy in deployment:
        v1app.delete_namespaced_deployment(deploy.metadata.name, deploy.metadata.namespace)


async def test_nginx_ipv6_only(deploy_ipv6_only, api_client: ApiClient):
    """Test that ipv6-only is enabled."""
    _, services = deploy_ipv6_only
    assert services, "No services created from ipv6-only nginx yaml"
    name, namespace = services[0].metadata.name, services[0].metadata.namespace
    svc = wait_for_nginx_service(api_client, name, namespace)
    assert svc.spec.cluster_i_ps, "Service does not have cluster IPs"
    as_obj = [ipaddress.ip_address(ip) for ip in svc.spec.cluster_i_ps]
    assert not any(ip.version == 4 for ip in as_obj), "IPv4 found in cluster IPs"
    assert any(ip.version == 6 for ip in as_obj), "IPv6 not found in cluster IPs"
