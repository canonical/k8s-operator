# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests for pki module."""

from cryptography.hazmat.backends import default_backend
from cryptography.x509 import load_pem_x509_certificate
from pki import extract_sans_from_cert


def test_extract_sans_from_cert():
    """Test extract_sans_from_cert function."""
    cert_data = b"""-----BEGIN CERTIFICATE-----
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

    cert = load_pem_x509_certificate(cert_data, default_backend())
    dns_names, ip_addresses = extract_sans_from_cert(cert)

    exp_dns_names = [
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "kubernetes.default.svc.cluster",
        "kubernetes.default.svc.cluster.local",
    ]
    exp_ip_addrs = [
        "10.97.72.214",
        "10.152.183.1",
        "127.0.0.1",
        "10.97.72.214",
        "::1",
        "fe80::216:3eff:fed6:9e71",
    ]

    assert len(dns_names) == len(exp_dns_names)
    assert len(ip_addresses) == len(exp_ip_addrs)

    for dns_name in dns_names:
        assert dns_name in exp_dns_names
    for ip_addr in ip_addresses:
        assert str(ip_addr) in exp_ip_addrs
