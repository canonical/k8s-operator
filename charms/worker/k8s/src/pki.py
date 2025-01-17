#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""A module providing PKI related functionalities."""

import ipaddress
import logging
import os
import typing

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.extensions import ExtensionNotFound
from literals import APISERVER_CERT

_IPAddressTypes = typing.Union[
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
]

log = logging.getLogger(__name__)


def get_api_server_cert() -> x509.Certificate:
    """Retrieve the API server certificate from the specified file path.

    Returns:
        `x509.Certificate`: The certificate object.

    Raises:
        FileNotFoundError: If the certificate file does not exist.
    """
    if not os.path.exists(APISERVER_CERT):
        raise FileNotFoundError(f"Certificate file not found: {APISERVER_CERT}")

    with open(APISERVER_CERT, "rb") as f:
        cert_data = f.read()

    return x509.load_pem_x509_certificate(cert_data, default_backend())


def extract_sans_from_cert(cert: x509.Certificate) -> tuple[list[str], list[_IPAddressTypes]]:
    """Extract the Subject Alternative Name (SAN) extension from the certificate.

    Args:
        cert (`x509.Certificate`): The certificate to extract the SAN extension from.

    Returns:
        `tuple[list[str], list[_IPAddressTypes]]`: A tuple containing the DNS names
        and IP addresses extracted from the SAN extension.
    """
    dns_names: list[str] = []
    ip_addresses: list[_IPAddressTypes] = []
    try:
        san_extension = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns_names = san_extension.get_values_for_type(x509.DNSName)
        ip_addresses = san_extension.get_values_for_type(x509.IPAddress)
    except ExtensionNotFound as e:
        log.warning(f"Subject Alternative Name (SAN) extension not found in the certificate: {e}")

    return dns_names, ip_addresses
