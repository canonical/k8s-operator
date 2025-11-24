#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import base64
import json
from platform import machine
from typing import Literal

import pytest
from helpers import ready_nodes
from juju import application, model, unit

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-charmed-etcd.yaml", apps_local=["k8s", "k8s-worker"])
]


async def get_etcd_certificate_from_k8s(
    kubernetes_cluster: model.Model, certificate: Literal["client", "ca"] = "client"
):
    """Get the certificate from a k8s unit."""
    k8s_unit: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    event = await k8s_unit.run(f"sudo cat /etc/kubernetes/pki/etcd/{certificate}.crt")
    result = await event.wait()
    if result.status != "completed":
        raise RuntimeError("Failed to get certificate from k8s")
    cert = result.results["stdout"].strip()
    assert cert, "Certificate is empty"
    return cert


async def get_client_cas_etcd(kubernetes_cluster: model.Model):
    """Get the client CA from the etcd unit."""
    etcd: unit.Unit = kubernetes_cluster.applications["charmed-etcd"].units[0]
    event = await etcd.run("sudo cat /var/snap/charmed-etcd/current/tls/client_ca.pem")
    result = await event.wait()
    if result.status != "completed":
        raise RuntimeError("Failed to get certificate from etcd")
    etcd_client_cas = result.results["stdout"].strip()
    assert etcd_client_cas, "etcd client CA is empty"
    return etcd_client_cas


async def assert_cluster_ready(kubernetes_cluster: model.Model):
    """Assert that the k8s cluster is ready."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"


async def get_etcd_tls_ca(kubernetes_cluster: model.Model):
    """Get the etcd TLS CA from the secrets."""
    secrets = await kubernetes_cluster.list_secrets(show_secrets=True)
    assert secrets, "No secrets found in the model"
    etcd_tls_ca_secret = next(
        (
            s
            for s in secrets
            if s.owner_tag == "application-charmed-etcd" and "tls-ca" in s.value.data
        ),
        None,
    )
    assert etcd_tls_ca_secret, "etcd TLS CA secret not found"
    tls_ca = base64.b64decode(etcd_tls_ca_secret.value.data["tls-ca"]).decode("utf-8")
    assert tls_ca, "etcd TLS CA is empty"
    return tls_ca


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


@pytest.mark.abort_on_fail
async def test_charmed_etcd_datastore(kubernetes_cluster: model.Model):
    """Test that etcd is the backend datastore."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    etcd: unit.Unit = kubernetes_cluster.applications["charmed-etcd"].units[0]
    etcd_port = etcd.safe_data["ports"][0]["number"]
    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert f"https://{etcd.public_address}:{etcd_port}" in status["datastore"]["servers"]


@pytest.mark.abort_on_fail
async def test_update_etcd_cluster(kubernetes_cluster: model.Model):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    etcd = kubernetes_cluster.applications["charmed-etcd"]
    count = 3 - len(etcd.units)
    if count > 0:
        await etcd.add_unit(count=count)

    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)

    expected_servers = []
    for u in etcd.units:
        etcd_port = u.safe_data["ports"][0]["number"]
        expected_servers.append(f"https://{u.public_address}:{etcd_port}")

    event = await k8s.run("k8s status --output-format json")
    result = await event.wait()
    status = json.loads(result.results["stdout"])
    assert status["ready"], "Cluster isn't ready"
    assert status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(status["datastore"]["servers"]) == set(expected_servers)


@pytest.mark.abort_on_fail
async def test_certificate_rotation(kubernetes_cluster: model.Model):
    """Test apiserver certificate rotation."""
    # Retrieve initial k8s client certificate and verify it's trusted by etcd
    initial_k8s_client_cert = await get_etcd_certificate_from_k8s(kubernetes_cluster)
    initial_etcd_client_ca_bundle = await get_client_cas_etcd(kubernetes_cluster)
    assert initial_k8s_client_cert in initial_etcd_client_ca_bundle, (
        "Initial k8s client certificate not found in etcd's client CA bundle"
    )

    # Verify initial CA certificate synchronization between k8s and etcd
    initial_etcd_tls_ca = await get_etcd_tls_ca(kubernetes_cluster)
    assert initial_etcd_tls_ca, "Initial etcd TLS CA certificate is empty"
    initial_k8s_etcd_ca = await get_etcd_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert initial_k8s_etcd_ca, "Initial k8s etcd CA certificate is empty"
    assert initial_etcd_tls_ca == initial_k8s_etcd_ca, (
        "Initial etcd TLS CA does not match k8s etcd CA certificate"
    )

    # Trigger certificate rotation by changing CA configuration
    ssc: application.Application = kubernetes_cluster.applications["self-signed-certificates"]
    await ssc.set_config({"ca-common-name": "NEW_CN_CA"})

    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)

    # Verify k8s client certificate has been rotated and is now trusted by etcd
    rotated_k8s_client_cert = await get_etcd_certificate_from_k8s(kubernetes_cluster)
    rotated_etcd_client_ca_bundle = await get_client_cas_etcd(kubernetes_cluster)
    assert rotated_k8s_client_cert != initial_k8s_client_cert, (
        "k8s client certificate was not rotated after CA change"
    )
    assert rotated_k8s_client_cert in rotated_etcd_client_ca_bundle, (
        "Rotated k8s client certificate not found in etcd's client CA bundle"
    )
    assert initial_k8s_client_cert not in rotated_etcd_client_ca_bundle, (
        "Initial k8s client certificate still present in etcd's client CA bundle after rotation"
    )
    await assert_cluster_ready(kubernetes_cluster)

    # Verify CA certificate synchronization after rotation
    rotated_etcd_tls_ca = await get_etcd_tls_ca(kubernetes_cluster)
    assert rotated_etcd_tls_ca, "Rotated etcd TLS CA certificate is empty"
    rotated_k8s_etcd_ca = await get_etcd_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert rotated_k8s_etcd_ca, "Rotated k8s etcd CA certificate is empty"
    assert rotated_etcd_tls_ca == rotated_k8s_etcd_ca, (
        "Rotated etcd TLS CA does not match rotated k8s etcd CA certificate"
    )

    await assert_cluster_ready(kubernetes_cluster)


@pytest.mark.abort_on_fail
async def test_both_charmed_and_legacy_etcd_integrated(kubernetes_cluster: model.Model):
    """Test that both charmed and legacy etcd can be integrated."""
    platforms = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }
    platform = platforms[machine()]
    await kubernetes_cluster.deploy(
        "etcd", channel="stable", application_name="legacy-etcd", constraints=f"arch={platform}"
    )
    await kubernetes_cluster.deploy(
        "easyrsa", channel="stable", application_name="easyrsa", constraints=f"arch={platform}"
    )
    await kubernetes_cluster.integrate("legacy-etcd", "easyrsa:client")
    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)
    await kubernetes_cluster.integrate("legacy-etcd", "k8s:etcd")
    await kubernetes_cluster.wait_for_idle(apps=["k8s"], status="blocked", timeout=20 * 60)

    await kubernetes_cluster.remove_application("legacy-etcd")
    await kubernetes_cluster.remove_application("easyrsa")
    await kubernetes_cluster.wait_for_idle(apps=["k8s"], status="active", timeout=20 * 60)


@pytest.mark.abort_on_fail
async def test_remove_charmed_etcd_integration(kubernetes_cluster: model.Model):
    """Test removing the charmed etcd integration."""
    k8s_app: application.Application = kubernetes_cluster.applications["k8s"]
    await k8s_app.remove_relation("k8s:etcd-client", "charmed-etcd:etcd-client")

    await kubernetes_cluster.wait_for_idle(apps=["k8s"], status="blocked", timeout=20 * 60)
