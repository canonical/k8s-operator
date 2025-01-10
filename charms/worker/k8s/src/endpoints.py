#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Helper functions to handle endpoints for the charm."""

import logging
import re
from ipaddress import ip_address
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def parse_endpoint(ep: str) -> tuple:
    """Split the given endpoint into its components.

    Args:
        ep (str): The endpoint to split.

    Returns:
        tuple: A tuple containing the scheme, address, port, and is_ipv6 of the endpoint.
    """
    ep = ep.strip()

    scheme = ""
    if re.match(r"^[a-zA-Z]+://", ep):
        scheme, _ = ep.split("://")

    parsed = urlparse(ep if scheme else f"placeholder://{ep}")
    netloc = parsed.netloc

    ip, port, is_ipv6 = ep.split("://")[1] if scheme else ep, "", False

    if ":" in netloc:
        # it's either ipv6 or has port or both
        if netloc.startswith("["):
            # ipv6 with braces (with or without port)
            is_ipv6 = True
            # fmt: off
            ip = netloc[netloc.index("[") + 1: netloc.index("]")]
            if netloc[netloc.index("]") + 1:].startswith(":"):
                # ipv6 with braces and port
                port = netloc[netloc.index("]") + 2:]
            # fmt: on
        else:
            # either ipv6 without braces or ipv4+port
            if netloc.count(":") > 1:
                # ipv6 without braces and without port.
                # an ipv6 without braces but with port is technically indiscriminable
                # from another ipv6 without port so we don't consider it.
                is_ipv6 = True
                ip = netloc
            else:
                # ipv4+port
                ip, port = netloc.split(":")

    try:
        ipa = ip_address(ip)
        if (ipa.version == 6) != is_ipv6:
            log.warning(f"IP version mismatch for {ip}, {ipa.version=}, {is_ipv6=}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warning(f"failed to validate {ip}: {e}")

    return scheme, ip, port, is_ipv6


def build_url(addr: str, new_port: str, new_scheme: str) -> str:
    """Construct a new URL by replacing the scheme and port of the given address.

    Args:
        addr (str): The original address which may include a scheme and port.
        new_port (str): The new port to be used in the constructed URL.
        new_scheme (str): The new scheme to be used in the constructed URL.

    Returns:
        str: The newly constructed URL with the specified scheme and port.
    """
    addr, new_port, new_scheme = addr.strip(), new_port.strip(), new_scheme.strip()
    scheme, ip, port, is_ipv6 = parse_endpoint(addr)

    if scheme:
        log.info(f"replacing already available scheme {scheme} in {addr=} with {new_scheme}")
    if port:
        log.info(f"replacing already available port {port} in {addr=} with {new_port}")
    if is_ipv6:
        ip = f"[{ip}]"

    return f"{new_scheme}://{ip}:{new_port}"
