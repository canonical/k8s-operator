#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import json
from typing import Literal

import jubilant
import pytest
from cloud import cloud_arch
from helpers import get_leader, ready_nodes, unit_names, unit_port, wait_active, wait_blocked

# NOTE: (mateo) Skipping entire module until Charmed Etcd support is added
pytest.skip("Skipping: Charmed Etcd support not yet stable", allow_module_level=True)

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with etcd, for all the test within this module.
APPS = ["k8s", "k8s-worker"]
pytestmark = [pytest.mark.bundle(file="test-bundle-charmed-etcd.yaml", apps_local=APPS)]

TWENTY_MIN = 20 * 60


def get_certificate_from_k8s(
    k8s_cluster: jubilant.Juju, certificate: Literal["client", "ca"] = "client"
) -> str:
    """Get a certificate from a k8s unit.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        certificate: Which certificate to read.

    Returns:
        The PEM-encoded certificate.
    """
    unit = unit_names(k8s_cluster, "k8s")[0]
    cert = k8s_cluster.exec(
        f"sudo cat /etc/kubernetes/pki/etcd/{certificate}.crt", unit=unit
    ).stdout.strip()
    assert cert, "Certificate is empty"
    return cert


def get_client_cas_etcd(k8s_cluster: jubilant.Juju) -> str:
    """Get the client CA from the etcd unit.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.

    Returns:
        The PEM-encoded client CA bundle.
    """
    unit = unit_names(k8s_cluster, "charmed-etcd")[0]
    etcd_client_cas = k8s_cluster.exec(
        "sudo cat /var/snap/charmed-etcd/current/tls/client_ca.pem", unit=unit
    ).stdout.strip()
    assert etcd_client_cas, "etcd client CA is empty"
    return etcd_client_cas


def assert_cluster_ready(k8s_cluster: jubilant.Juju) -> None:
    """Assert that the k8s cluster is ready.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
    """
    unit = unit_names(k8s_cluster, "k8s")[0]
    status = json.loads(k8s_cluster.exec("k8s status --output-format json", unit=unit).stdout)
    assert status["ready"], "Cluster isn't ready"


def get_etcd_tls_ca(k8s_cluster: jubilant.Juju) -> str:
    """Get the etcd TLS CA from the model secrets.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.

    Returns:
        The PEM-encoded TLS CA.
    """
    secrets = k8s_cluster.secrets()
    assert secrets, "No secrets found in the model"
    for secret in secrets:
        if secret.owner not in ("charmed-etcd", "application-charmed-etcd"):
            continue
        revealed = k8s_cluster.show_secret(secret.uri, reveal=True)
        # juju show-secret --reveal returns plaintext; libjuju returned base64.
        if tls_ca := revealed.content.get("tls-ca"):
            assert tls_ca, "etcd TLS CA is empty"
            return tls_ca
    pytest.fail("etcd TLS CA secret not found")


def test_nodes_ready(k8s_cluster: jubilant.Juju):
    """Deploy the charm and wait for active/idle status."""
    status = k8s_cluster.status()
    expected_nodes = sum(len(status.get_units(app)) for app in APPS)
    ready_nodes(k8s_cluster, get_leader(k8s_cluster, "k8s"), expected_nodes)


def test_charmed_etcd_datastore(k8s_cluster: jubilant.Juju):
    """Test that etcd is the backend datastore."""
    status = k8s_cluster.status()
    etcd_unit = unit_names(k8s_cluster, "charmed-etcd")[0]
    address = status.get_units("charmed-etcd")[etcd_unit].public_address
    expected = f"https://{address}:{unit_port(status, 'charmed-etcd', etcd_unit)}"

    unit = unit_names(k8s_cluster, "k8s")[0]
    k8s_status = json.loads(k8s_cluster.exec("k8s status --output-format json", unit=unit).stdout)
    assert k8s_status["ready"], "Cluster isn't ready"
    assert k8s_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert expected in k8s_status["datastore"]["servers"]


def test_update_etcd_cluster(k8s_cluster: jubilant.Juju):
    """Test that adding etcd clusters are propagated to the k8s cluster."""
    count = 3 - len(k8s_cluster.status().get_units("charmed-etcd"))
    if count > 0:
        k8s_cluster.add_unit("charmed-etcd", num_units=count)
    wait_active(k8s_cluster, timeout=TWENTY_MIN)

    status = k8s_cluster.status()
    expected_servers = {
        f"https://{unit.public_address}:{unit_port(status, 'charmed-etcd', name)}"
        for name, unit in status.get_units("charmed-etcd").items()
    }

    unit = unit_names(k8s_cluster, "k8s")[0]
    k8s_status = json.loads(k8s_cluster.exec("k8s status --output-format json", unit=unit).stdout)
    assert k8s_status["ready"], "Cluster isn't ready"
    assert k8s_status["datastore"]["type"] == "external", "Not bootstrapped against etcd"
    assert set(k8s_status["datastore"]["servers"]) == expected_servers


def test_certificate_rotation_k8s(k8s_cluster: jubilant.Juju):
    """Test apiserver certificate rotation."""
    old_cert_k8s = get_certificate_from_k8s(k8s_cluster)
    old_client_cas_etcd = get_client_cas_etcd(k8s_cluster)
    assert old_cert_k8s in old_client_cas_etcd, "Old cert not in etcd client CA"

    k8s_cluster.config("ssc-k8s", {"ca-common-name": "NEW_CN_CA"})
    wait_active(k8s_cluster, timeout=TWENTY_MIN)

    new_cert_k8s = get_certificate_from_k8s(k8s_cluster)
    new_client_cas_etcd = get_client_cas_etcd(k8s_cluster)
    assert new_cert_k8s != old_cert_k8s, "Certificate did not rotate"
    assert new_cert_k8s in new_client_cas_etcd, "New cert not in etcd client CA"
    assert old_cert_k8s not in new_client_cas_etcd, "Old cert still in etcd client CA"
    assert_cluster_ready(k8s_cluster)


def test_certificate_rotation_etcd(k8s_cluster: jubilant.Juju):
    """Test etcd TLS CA rotation."""
    current_etcd_tls_ca = get_etcd_tls_ca(k8s_cluster)
    assert current_etcd_tls_ca, "Current etcd TLS CA is empty"
    current_k8s_client_ca = get_certificate_from_k8s(k8s_cluster, certificate="ca")
    assert current_k8s_client_ca, "Current k8s client CA is empty"
    assert current_etcd_tls_ca == current_k8s_client_ca, "etcd TLS CA does not match k8s client CA"

    k8s_cluster.config("ssc-charmed-etcd", {"ca-common-name": "NEW_ETCD_CN_CA"})
    wait_active(k8s_cluster, timeout=TWENTY_MIN)

    new_etcd_tls_ca = get_etcd_tls_ca(k8s_cluster)
    assert new_etcd_tls_ca, "New etcd TLS CA is empty"
    new_k8s_client_ca = get_certificate_from_k8s(k8s_cluster, certificate="ca")
    assert new_k8s_client_ca, "New k8s client CA is empty"
    assert new_etcd_tls_ca == new_k8s_client_ca, "New etcd TLS CA does not match new k8s client CA"

    assert_cluster_ready(k8s_cluster)


def test_both_charmed_and_legacy_etcd_integrated(k8s_cluster: jubilant.Juju):
    """Test that both charmed and legacy etcd can be integrated."""
    arch = cloud_arch(k8s_cluster.show_model().controller_name)
    k8s_cluster.deploy("etcd", "legacy-etcd", channel="stable", constraints={"arch": arch})
    k8s_cluster.deploy("easyrsa", "easyrsa", channel="stable", constraints={"arch": arch})
    k8s_cluster.integrate("legacy-etcd", "easyrsa:client")
    wait_active(k8s_cluster, timeout=TWENTY_MIN)

    k8s_cluster.integrate("legacy-etcd", "k8s:etcd")
    wait_blocked(k8s_cluster, "k8s", timeout=TWENTY_MIN)

    k8s_cluster.remove_application("legacy-etcd", "easyrsa")
    wait_active(k8s_cluster, "k8s", timeout=TWENTY_MIN)


def test_remove_charmed_etcd_integration(k8s_cluster: jubilant.Juju):
    """Test removing the charmed etcd integration."""
    k8s_cluster.remove_relation("k8s:etcd-client", "charmed-etcd:etcd-client")
    wait_blocked(k8s_cluster, "k8s", timeout=TWENTY_MIN)
