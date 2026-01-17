# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""A module providing PKI related functionalities."""

import logging
import os
from pathlib import Path
from typing import List, Tuple, Union

from cryptography import x509
from literals import PKI_DIR

log = logging.getLogger(__name__)


def get_certificate_sans(cert_path: Union[str, Path]) -> Tuple[List[str], List[str]]:
    """Extract the DNS and IP Subject Alternative Names (SANs) from a given certificate file.

    This function uses the cryptography library to extract the SANs from the certificate file.

    Args:
        cert_path (Union[str, Path]): The path to the certificate file.

    Returns:
        Tuple[List[str], List[str]]: A tuple containing two lists:
            - The first list contains DNS SANs.
            - The second list contains IP SANs.
    """
    cert_path = Path(cert_path) if isinstance(cert_path, str) else cert_path

    if not cert_path.exists():
        log.warning("Certificate file not found: %s", cert_path)
        return [], []

    with open(cert_path, "rb") as file:
        cert_data = file.read()

    try:
        certificate = x509.load_pem_x509_certificate(cert_data)

        san_extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        sans = san_extension.value

        dns_sans = set(sans.get_values_for_type(x509.DNSName))
        ip_address = {str(ip) for ip in sans.get_values_for_type(x509.IPAddress)}
    except ValueError:
        log.exception("Failed to parse certificate %s", cert_path)
        return [], []
    except x509.ExtensionNotFound:
        log.exception("No SAN extension found in certificate %s", cert_path)
        return [], []

    return sorted(dns_sans), sorted(ip_address)


def check_ca_key():
    """Check if the CA key exists.

    Returns:
        bool: True if the CA key exists, False otherwise.
    """
    return os.path.isfile(PKI_DIR / "ca.key")
