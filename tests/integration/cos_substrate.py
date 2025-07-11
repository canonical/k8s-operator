# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aids in testing COS substrate on LXD."""

import ipaddress
import logging
import time
from typing import Tuple, Union

from .lxd_substrate import LXDSubstrate

log = logging.getLogger(__name__)
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class COSSubstrate(LXDSubstrate):
    """A LXD substrate."""

    def __init__(self, container_name: str, network_name: str) -> None:
        """Initialize LXDSubstrate instance.

        Args:
            container_name (str): Name of the container.
            network_name (str): Name of the network.
        """
        super().__init__()
        self.container_name = container_name
        self.network_name = network_name

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
            RuntimeError: when the container's snapd fails to load seed
        """
        self.apply_profile(["microk8s.profile"], "cos-profile")
        reserved_start, reserved_stop = self.create_network(self.network_name)
        container = self.create_container(self.container_name, self.network_name)
        max_attempts, sleep_duration = 10, 30
        for _ in range(max_attempts):
            rc, _, _ = self.execute_command(container, ["snap", "wait", "system", "seed.loaded"])
            if rc == 0:
                break
            time.sleep(sleep_duration)
        else:
            raise RuntimeError("Failed to wait for system seed")

        self.install_k8s(container)
        self.enable_microk8s_addons(container, f"{reserved_start}-{reserved_stop}")
        return self.get_kubeconfig(container)

    def enable_microk8s_addons(self, container, ranges: str):
        """Enable MicroK8s addons.

        Args:
            container: Container instance.
            ranges (str): MetalLB IP ranges.
        """
        addons = ["dns", "hostpath-storage", f"metallb:{ranges}"]
        for addon in addons:
            self.execute_command(container, ["sudo", "microk8s", "enable", addon])

    def get_kubeconfig(self, container) -> bytes:
        """Get kubeconfig from a container.

        Args:
            container: Container instance.

        Returns:
            str: The kubeconfig.
        """
        rc, stdout, stderr = self.execute_command(container, ["microk8s", "config"])
        if rc != 0:
            log.error("Failed to get kubeconfig: %s, %s", stdout, stderr)
        return stdout

    def install_k8s(self, container):
        """Install Kubernetes inside a container.

        Args:
            container: Container instance.

        Returns:
            Tuple[str, bytes, bytes]: rc, stdout, stderr
        """
        return self.execute_command(
            container,
            ["sudo", "snap", "install", "microk8s", "--channel=1.28/stable", "--classic"],
        )

    def teardown_substrate(self):
        """Teardown the COS substrate."""
        container = self.client.instances.get(self.container_name)
        self.delete_container(container)
        self.delete_network(self.network_name)
        self.remove_profile()
