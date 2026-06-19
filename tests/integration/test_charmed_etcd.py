#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import base64
import json
from platform import machine
from typing import Literal

import jubilant
import pytest
from helpers import ready_nodes

# NOTE: (mateo) Skipping entire module until Charmed Etcd support is added
pytest.skip("Skipping: Charmed Etcd support not yet stable", allow_module_level=True)

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle-charmed-etcd.yaml", apps_local=["k8s", "k8s-worker"])
]


def get_certificate_from_k8s(juju: jubilant.Juju, certificate: Literal["client", "ca"] = "client"):
    """Get the certificate from a k8s unit."""
    k8s_unit = next(iter(juju.status().get_units("k8s")))
    task = juju.exec(f"sudo cat /etc/kubernetes/pki/etcd/{certificate}.crt", unit=k8s_unit)
    cert = task.stdout.strip()
    assert cert, "Certificate is empty"
    return cert


def get_client_cas_etcd(juju: jubilant.Juju):
    """Get the client CA from the etcd unit."""
    etcd_unit = next(iter(juju.status().get_units("charmed-etcd")))
    task = juju.exec("sudo cat /var/snap/charmed-etcd/current/tls/client_ca.pem", unit=etcd_unit)
    etcd_client_cas = task.stdout.strip()
    assert etcd_client_cas, "etcd client CA is empty"
    return etcd_client_cas


def assert_cluster_ready(juju: jubilant.Juju):
    """Assert that the k8s cluster is ready."""
    k8s_unit = next(iter(juju.status().get_units("k8s")))
    task = juju.exec("k8s status --output-format json", unit=k8s_unit)
    status = json.loads(task.stdout)
    assert status["ready"], "Cluster isn't ready"


def get_etcd_tls_ca(juju: jubilant.Juju):
    """Get the etcd TLS CA from the secrets."""
    secrets = juju.secrets()
    assert secrets, "No secrets found in the model"
    for secret in secrets:
        if secret.owner != "charmed-etcd":
            continue
        revealed = juju.show_secret(secret.uri, reveal=True)
        if "tls-ca" in revealed.content:
            tls_ca = base64.b64decode(revealed.content["tls-ca"]).decode("utf-8")
            assert tls_ca, "etcd TLS CA is empty"
            return tls_ca
    raise AssertionError("etcd TLS CA secret not found")


@pytest.mark.abort_on_fail
def test_nodes_ready(kubernetes_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = kubernetes_cluster.status()
    expected_nodes = len(status.get_units("k8s")) + len(status.get_units("k8s-worker"))
    k8s_unit = next(iter(status.get_units("k8s")))
    ready_nodes(kubernetes_cluster, k8s_unit, expected_nodes)


@pytest.mark.abort_on_fail
def test_charmed_etcd_datastore(kubernetes_cluster: jubilant.Juju):
    """Test that etcd is the backend datastore."""
    status = kubernetes_cluster.status()
    k8s_unit = next(iter(status.get_units("k8s")))
    etcd_unit = next(iter(status.get_units("charmed-etcd").values()))
    etcd_port = int(etcd_unit.open_ports[0].split("/")[0])
    task = kubernetes_cluster.exec("k8s status --output-format json", unit=k8s_unit)
    cluster_status = json.loads(task.stdout)
    assert cluster_status["ready"], "Cluster isn't ready"
    assert cluster_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert (
        f"https://{etcd_unit.public_address}:{etcd_port}" in cluster_status["datastore"]["servers"]
    )


@pytest.mark.abort_on_fail
def test_update_etcd_cluster(kubernetes_cluster: jubilant.Juju):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    etcd_units = kubernetes_cluster.status().get_units("charmed-etcd")
    count = 3 - len(etcd_units)
    if count > 0:
        kubernetes_cluster.add_unit("charmed-etcd", num_units=count)

    kubernetes_cluster.wait(jubilant.all_active, timeout=20 * 60)

    status = kubernetes_cluster.status()
    expected_servers = []
    for u in status.get_units("charmed-etcd").values():
        etcd_port = int(u.open_ports[0].split("/")[0])
        expected_servers.append(f"https://{u.public_address}:{etcd_port}")

    k8s_unit = next(iter(status.get_units("k8s")))
    task = kubernetes_cluster.exec("k8s status --output-format json", unit=k8s_unit)
    cluster_status = json.loads(task.stdout)
    assert cluster_status["ready"], "Cluster isn't ready"
    assert cluster_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(cluster_status["datastore"]["servers"]) == set(expected_servers)


@pytest.mark.abort_on_fail
def test_certificate_rotation_k8s(kubernetes_cluster: jubilant.Juju):
    """Test apiserver certificate rotation."""
    old_cert_k8s = get_certificate_from_k8s(kubernetes_cluster)
    old_client_cas_etcd = get_client_cas_etcd(kubernetes_cluster)
    assert old_cert_k8s in old_client_cas_etcd, "Old cert not in etcd client CA"

    kubernetes_cluster.config("ssc-k8s", {"ca-common-name": "NEW_CN_CA"})

    kubernetes_cluster.wait(jubilant.all_active, timeout=20 * 60)

    new_cert_k8s = get_certificate_from_k8s(kubernetes_cluster)
    new_client_cas_etcd = get_client_cas_etcd(kubernetes_cluster)
    assert new_cert_k8s != old_cert_k8s, "Certificate did not rotate"
    assert new_cert_k8s in new_client_cas_etcd, "New cert not in etcd client CA"
    assert old_cert_k8s not in new_client_cas_etcd, "Old cert still in etcd client CA"
    assert_cluster_ready(kubernetes_cluster)


@pytest.mark.abort_on_fail
def test_certificate_rotation_etcd(kubernetes_cluster: jubilant.Juju):
    """Test etcd TLS CA rotation."""
    current_etcd_tls_ca = get_etcd_tls_ca(kubernetes_cluster)
    assert current_etcd_tls_ca, "Current etcd TLS CA is empty"
    current_k8s_client_ca = get_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert current_k8s_client_ca, "Current k8s client CA is empty"
    assert current_etcd_tls_ca == current_k8s_client_ca, "etcd TLS CA does not match k8s client CA"

    kubernetes_cluster.config("ssc-charmed-etcd", {"ca-common-name": "NEW_ETCD_CN_CA"})

    kubernetes_cluster.wait(jubilant.all_active, timeout=20 * 60)

    new_etcd_tls_ca = get_etcd_tls_ca(kubernetes_cluster)
    assert new_etcd_tls_ca, "New etcd TLS CA is empty"
    new_k8s_client_ca = get_certificate_from_k8s(kubernetes_cluster, certificate="ca")
    assert new_k8s_client_ca, "New k8s client CA is empty"
    assert new_etcd_tls_ca == new_k8s_client_ca, "New etcd TLS CA does not match new k8s client CA"

    assert_cluster_ready(kubernetes_cluster)


@pytest.mark.abort_on_fail
def test_both_charmed_and_legacy_etcd_integrated(kubernetes_cluster: jubilant.Juju):
    """Test that both charmed and legacy etcd can be integrated."""
    platforms = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }
    platform = platforms[machine()]
    kubernetes_cluster.deploy(
        "etcd", "legacy-etcd", channel="stable", constraints={"arch": platform}
    )
    kubernetes_cluster.deploy(
        "easyrsa", "easyrsa", channel="stable", constraints={"arch": platform}
    )
    kubernetes_cluster.integrate("legacy-etcd", "easyrsa:client")
    kubernetes_cluster.wait(jubilant.all_active, timeout=20 * 60)
    kubernetes_cluster.integrate("legacy-etcd", "k8s:etcd")
    kubernetes_cluster.wait(lambda s: jubilant.all_blocked(s, "k8s"), timeout=20 * 60)

    kubernetes_cluster.remove_application("legacy-etcd")
    kubernetes_cluster.remove_application("easyrsa")
    kubernetes_cluster.wait(lambda s: jubilant.all_active(s, "k8s"), timeout=20 * 60)


@pytest.mark.abort_on_fail
def test_remove_charmed_etcd_integration(kubernetes_cluster: jubilant.Juju):
    """Test removing the charmed etcd integration."""
    kubernetes_cluster.remove_relation("k8s:etcd-client", "charmed-etcd:etcd-client")

    kubernetes_cluster.wait(lambda s: jubilant.all_blocked(s, "k8s"), timeout=20 * 60)
