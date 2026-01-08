# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aids in testing COS substrate on LXD."""

import gzip
import ipaddress
import json
import logging
import lzma
import re
import shlex
import time
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple, Union

from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

from .lxd_substrate import LXDSubstrate, VMOptions

log = logging.getLogger(__name__)
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
LXDExceptions = (NotFound, LXDAPIException, ClientConnectionFailed)
TEST_DATA = Path(__file__).parent / "data"
K8S_PROFILE_URL = "https://raw.githubusercontent.com/canonical/k8s-snap/refs/heads/main/tests/integration/lxd-profile.yaml"


def _gzip_to_xz(input_bytes: bytes) -> bytes:
    """Convert gzip bytes to xz compressed bytes."""
    xz_buffer = BytesIO()
    with gzip.GzipFile(fileobj=BytesIO(input_bytes)) as gz:
        with lzma.LZMAFile(xz_buffer, mode="wb") as xz_file:
            decompressed_data = gz.read()
            xz_file.write(decompressed_data)

    return xz_buffer.getvalue()


class COSSubstrate(LXDSubstrate):
    """A COS Substrate."""

    def __init__(self, vm: Optional[VMOptions] = None) -> None:
        super().__init__(vm)
        self.instance_name = "cos-substrate"
        self.network_name = "cos-network"

    def create_network(
        self, network_name: str, subnet_cidr: str = "10.10.0.0/24", reserved_addresses: int = 5
    ) -> Tuple[IPAddress, IPAddress]:
        """Create a network.

        Args:
            network_name (str): Name of the network.
            subnet_cidr (Optional[str]): CIDR of the subnet. Defaults to '10.10.0.0/24'.
            reserved_addresses (Optional[int]): Number of reserved IP addresses. Defaults to 5.

        Raises:
            ValueError: when total_address is less than reserved_addresses

        Returns:
            Tuple[IPAddress, IPAddress]
        """
        existing_networks = self.client.networks.all()
        for net in existing_networks:
            if net.name == network_name:
                raise ValueError("Network already exists. Skipping creation.")

        subnet = ipaddress.ip_network(subnet_cidr)
        total_addresses = subnet.num_addresses - 2

        if reserved_addresses >= total_addresses:
            raise ValueError(
                "Reserved Addresses must be less than the total number of usable addresses."
            )

        gateway_ip = subnet.network_address + 1
        ipv4_address = f"{gateway_ip}/{subnet.prefixlen}"
        dhcp_range_start = subnet.network_address + 2
        dhcp_range_stop = subnet.network_address + total_addresses - reserved_addresses

        reserved_stop = subnet.broadcast_address - 1

        network_config = {
            "name": network_name,
            "description": "Custom LXD network for COS integration",
            "config": {
                "ipv4.address": ipv4_address,
                "ipv4.nat": "true",
                "ipv4.dhcp": "true",
                "ipv4.dhcp.ranges": f"{dhcp_range_start}-{dhcp_range_stop}",
                "ipv6.address": "none",
            },
            "type": "bridge",
        }

        log.info(
            "Creating network '%s' with %s reserved addresses.",
            network_name,
            reserved_addresses,
        )
        self.apply_networks(network_name, network_config)
        reserved_start = dhcp_range_stop + 1
        log.info("Reserved IP range: %s-%s", reserved_start, reserved_stop)
        return reserved_start, reserved_stop

    def create_substrate(self) -> bytes:
        """Create a COS substrate.

        Returns:
            bytes: The generated kubeconfig.

        Raises:
            RuntimeError: when the instance's snapd fails to load seed
        """
        self.apply_profile([], "cos-profile")
        reserved_start, reserved_stop = self.create_network(self.network_name)
        instance = self.create_instance(self.instance_name, self.network_name)
        max_attempts, sleep_duration = 10, 30
        for _ in range(max_attempts):
            rc, _, _ = self.execute_command(
                instance, ["snap", "wait", "system", "seed.loaded"], check=False
            )
            if rc == 0:
                break
            time.sleep(sleep_duration)
        else:
            raise RuntimeError("Failed to wait for system seed")

        self.bootstrap_k8s(instance, f"{reserved_start}-{reserved_stop}")
        self.ready_k8s(instance)
        return self.get_kubeconfig(instance)

    def bootstrap_k8s(self, instance, range: str):
        """Enable MicroK8s addons.

        Args:
            instance: Container instance.
            range (str): CIDR range for load balancer.
        """
        cidrs = json.dumps([range])
        commands = [
            "snap install k8s --classic",
            "k8s bootstrap",
            "k8s status --wait-ready",
            f"k8s set load-balancer.cidrs='{cidrs}'",
            "k8s enable load-balancer",
        ]
        for cmd in commands:
            self.execute_command(instance, shlex.split("sudo " + cmd))

    def ready_k8s(self, instance):
        """Wait for K8s to be ready.

        Args:
            instance: Container instance.
        """
        max_attempts, sleep_duration = 10, 30
        command = "k8s status --wait-ready"
        for _ in range(max_attempts):
            rc, stdout, stderr = self.execute_command(instance, shlex.split(command), check=False)
            if rc == 0 and b"failed" not in stdout.lower():
                break
            log.warning("K8s not ready yet, retrying...")
            log.info("K8s status output: %s", stdout.decode())
            log.info("K8s status errors: %s", stderr.decode())
            time.sleep(sleep_duration)
        else:
            raise RuntimeError("Failed to wait for system seed")

    def inspect_k8s(self, instance):
        """Inspect K8s status.

        Args:
            instance: Container instance.
        """
        command = "k8s inspect"
        rc, stdout, stderr = self.execute_command(instance, shlex.split(command), check=False)
        if rc != 0:
            log.error("K8s inspect failed with rc=%s", rc)
            log.error("K8s inspect stdout: %s", stdout.decode())
            log.error("K8s inspect stderr: %s", stderr.decode())
            return
        artifacts = re.findall(r"(\S+\.tar\.gz)", stdout.decode())
        log.info("K8s inspect artifacts: %s", list(artifacts))
        for artifact in artifacts:
            local = f"juju-crashdump-{self.instance_name}-{Path(artifact).stem}.xz"
            log.info("Retrieving artifact: %s to %s", artifact, local)
            with open(local, "wb") as f:
                xz = _gzip_to_xz(instance.files.get(artifact))
                f.write(xz)

    def get_kubeconfig(self, instance) -> bytes:
        """Get kubeconfig from a instance.

        Args:
            instance: Container instance.

        Returns:
            str: The kubeconfig.
        """
        command = "sudo k8s config"
        _, stdout, _ = self.execute_command(instance, shlex.split(command))
        return stdout

    def teardown_substrate(self):
        """Teardown the COS substrate."""
        instance = self.client.instances.get(self.instance_name)
        self.inspect_k8s(instance)
        self.delete_instance(instance)
        self.delete_network(self.network_name)
        if profile := self.profile_name:
            self.remove_profile(profile)
