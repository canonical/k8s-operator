# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utils module."""
import subprocess


def get_public_address() -> str:
    """Get public address from juju.

    Returns:
        (str) public ip address of the unit
    """
    cmd = ["unit-get", "public-address"]
    return subprocess.check_output(cmd).decode("UTF-8").strip()
