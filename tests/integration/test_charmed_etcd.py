#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import base64
import json
from typing import Literal

import pytest
from helpers import ready_nodes
from juju import application, model, unit

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-charmed-etcd.yaml", apps_local=["k8s", "k8s-worker"])
]


async def get_certificate_from_k8s(
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
async def test_etcd_datastore(kubernetes_cluster: model.Model):
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
async def test_certificate_rotation_k8s(kubernetes_cluster: model.Model):
    """Test apiserver certificate rotation."""
    old_cert_k8s = await get_certificate_from_k8s(kubernetes_cluster)
    old_client_cas_etcd = await get_client_cas_etcd(kubernetes_cluster)
    assert old_cert_k8s in old_client_cas_etcd, "Old cert not in etcd client CA"

    ssc_k8s: application.Application = kubernetes_cluster.applications["ssc-k8s"]
    await ssc_k8s.set_config({"ca-common-name": "NEW_CN_CA"})

    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)

    new_cert_k8s = await get_certificate_from_k8s(kubernetes_cluster)
    new_client_cas_etcd = await get_client_cas_etcd(kubernetes_cluster)
    assert new_cert_k8s != old_cert_k8s, "Certificate did not rotate"
    assert new_cert_k8s in new_client_cas_etcd, "New cert not in etcd client CA"
    assert old_cert_k8s not in new_client_cas_etcd, "Old cert still in etcd client CA"
    await assert_cluster_ready(kubernetes_cluster)


@pytest.mark.abort_on_fail
async def test_certificate_rotation_etcd(kubernetes_cluster: model.Model):
    """Test etcd TLS CA rotation."""
    current_etcd_tls_ca = await get_etcd_tls_ca(kubernetes_cluster)
    assert current_etcd_tls_ca, "Current etcd TLS CA is empty"
    current_k8s_client_ca = await get_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert current_k8s_client_ca, "Current k8s client CA is empty"
    assert current_etcd_tls_ca == current_k8s_client_ca, "etcd TLS CA does not match k8s client CA"

    ssc_etcd: application.Application = kubernetes_cluster.applications["ssc-charmed-etcd"]
    await ssc_etcd.set_config({"ca-common-name": "NEW_ETCD_CN_CA"})

    await kubernetes_cluster.wait_for_idle(status="active", timeout=20 * 60)

    new_etcd_tls_ca = await get_etcd_tls_ca(kubernetes_cluster)
    assert new_etcd_tls_ca, "New etcd TLS CA is empty"
    new_k8s_client_ca = await get_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert new_k8s_client_ca, "New k8s client CA is empty"
    assert new_etcd_tls_ca == new_k8s_client_ca, "New etcd TLS CA does not match new k8s client CA"

    await assert_cluster_ready(kubernetes_cluster)


@pytest.mark.abort_on_fail
async def test_both_charmed_and_legacy_etcd_integrated(kubernetes_cluster: model.Model):
    """Test that both charmed and legacy etcd can be integrated."""
    await kubernetes_cluster.deploy("etcd", channel="stable", application_name="legacy-etcd")
    await kubernetes_cluster.deploy("easyrsa", channel="stable", application_name="easyrsa")
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
