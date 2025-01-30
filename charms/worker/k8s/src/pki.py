# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""A module providing PKI related functionalities."""

import logging
import subprocess
from ipaddress import ip_address
from pathlib import Path
from typing import List, Set, Tuple, Union

log = logging.getLogger(__name__)


def get_certificate_sans(cert_path: Union[str, Path]) -> Tuple[List[str], List[str]]:
    """Extract the DNS and IP Subject Alternative Names (SANs) from a given certificate file.

    This function uses the openssl command to extract the SANs from the certificate file.

    Args:
        cert_path (Union[str, Path]): The path to the certificate file.

    Returns:
        tuple[list[str], list[str]]: A tuple containing two lists:
            - The first list contains DNS SANs.
            - The second list contains IP SANs.
    """
    try:
        cmd = f"openssl x509 -noout -ext subjectAltName -in {cert_path}".split()
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        log.error("Failed to call openssl for certificate SANs: %s", e)
        return [], []

    lines = result.stdout.splitlines()
    if len(lines) < 2:
        log.info("No SANs found in %s", cert_path)
        return [], []

    # lines[0] == "X509v3 Subject Alternative Name: "
    all_sans = [san.strip() for san in lines[1].split(",")]
    dns_sans: Set[str] = set()
    ip_sans: Set[str] = set()

    dns_prefix = "DNS:"
    ip_prefix = "IP Address:"
    for san in all_sans:
        # fmt: off
        if san.startswith(dns_prefix):
            dns_sans.add(san[len(dns_prefix):])
        elif san.startswith(ip_prefix):
            ip_str = san[len(ip_prefix):]
            try:
                ip = ip_address(ip_str)
                ip_sans.add(str(ip))
            except ValueError:
                log.warning("Invalid IP SAN: %s", ip_str)
        # fmt: on

    return list(dns_sans), list(ip_sans)
