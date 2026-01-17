# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests for pki module."""

import os
import tempfile

from pki import get_certificate_sans


def test_get_certificate_sans():
    """Test get_certificate_sans function."""
    exp_dns_sans = [
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "kubernetes.default.svc.cluster",
        "kubernetes.default.svc.cluster.local",
    ]
    exp_ip_sans = [
        "10.152.183.1",
        "127.0.0.1",
        "10.97.72.214",
        "::1",
        "fe80::216:3eff:fed6:9e71",
    ]

    with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as cert_file:
        cert_path = cert_file.name
        cert_content = """-----BEGIN CERTIFICATE-----
MIID6DCCAtCgAwIBAgIRAJ5lxXXSPlQqLz6uJzreDF0wDQYJKoZIhvcNAQELBQAw
GDEWMBQGA1UEAxMNa3ViZXJuZXRlcy1jYTAeFw0yNTAxMTcwODIwMTVaFw00NTAx
MTcwODIwMTVaMBkxFzAVBgNVBAMTDmt1YmUtYXBpc2VydmVyMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzldHfBkxh4RVBr21EpYAi8pcap9LzqCsxvR2
7kn/u3SVow5Z40p7aFEMDf9CCG/gx+5oyh55wXQ6QiypA2PLA2kyZDK0kCtSpWPa
yGWLjCyejdRFWa7LU3aKxzlza6Kluy0sPXRBRoL7YZ105mUkQOa5ioMJuKB9xJ8A
MFHNdss29VVE6XaB7ndZtHiEwTZcWXNJ9i0YFVJs2kouakHCxt0qldRrLsugltWo
hrb31GsayBIAb/JSPbH8Hky26G/8RvMkykpGNC9CrEbaPj0JOZApd79xmNngc6Kb
U7KkcuirAeCE8Uji6k82ah2jKsExC4LR72F0YTeMNMpRVVcOMQIDAQABo4IBKjCC
ASYwDgYDVR0PAQH/BAQDAgSwMB0GA1UdJQQWMBQGCCsGAQUFBwMCBggrBgEFBQcD
ATAMBgNVHRMBAf8EAjAAMB8GA1UdIwQYMBaAFO9Oss4CtGTt2bo6jYNXzZ7eZu6x
MIHFBgNVHREEgb0wgbqCCmt1YmVybmV0ZXOCEmt1YmVybmV0ZXMuZGVmYXVsdIIW
a3ViZXJuZXRlcy5kZWZhdWx0LnN2Y4Iea3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5j
bHVzdGVygiRrdWJlcm5ldGVzLmRlZmF1bHQuc3ZjLmNsdXN0ZXIubG9jYWyHBAph
SNaHBAqYtwGHBH8AAAGHBAphSNaHEAAAAAAAAAAAAAAAAAAAAAGHEP6AAAAAAAAA
AhY+//7WnnEwDQYJKoZIhvcNAQELBQADggEBACJNIU9CRSO8yXpXCpn5roFKf9YW
BpfYe3c0A6aqhm6dVqHs6NEpH5T2KCYp1Tg4HSaawNxLS2BImCqKVNc/PlOyehoY
FnWE4Kli2C4zUv272peJb2wRcZjnZjHV9+Xh3rSI3tbrEJHVK1tkjAfLaAffk6KB
jqaO1we99UxeuhkRh6W8t8ARY9BasQRloe53c/+bDw6WtftaWuHlXbb4s4gUh0Un
GMLPA6dh7pJFo4uolAtbYc4oE0FRUySPxoZzw5p/Mzt9Kj8omPgmP4Hb3D+Uml8P
Kryj6dPJQjiDEqlfZC/n0aR98onWgb1O4Xdkm4HT20/R4gUNTS0rM/k4wTY=
-----END CERTIFICATE-----"""
        cert_file.write(cert_content.encode())

    try:
        dns_sans, ip_sans = get_certificate_sans(cert_path)

        assert len(dns_sans) == len(set(dns_sans))
        assert len(ip_sans) == len(set(ip_sans))
        assert len(dns_sans) == len(exp_dns_sans)
        assert len(ip_sans) == len(exp_ip_sans)
        assert all(dns_name in exp_dns_sans for dns_name in dns_sans)
        assert all(ip_addr in exp_ip_sans for ip_addr in ip_sans)
    finally:
        os.remove(cert_path)
