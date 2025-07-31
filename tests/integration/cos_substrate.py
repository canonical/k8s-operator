# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aids in testing COS substrate on LXD."""

import ipaddress
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Protocol, Tuple, Union

import yaml
from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

log = logging.getLogger(__name__)
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
LXDExceptions = (NotFound, LXDAPIException, ClientConnectionFailed)


class COSSubstrate(Protocol):
    """Interface for managing a COS substrate."""

    def create_substrate(self) -> str:
        """Create a COS substrate."""

    def teardown_substrate(self):
        """Teardown the COS substrate."""


class LXDSubstrate(COSSubstrate):
    """A COS substrate implemented using LXD."""

    def __init__(self, container_name: str, network_name: str) -> None:
        """Initialize LXDSubstrate instance.

        Args:
            container_name (str): Name of the container.
            network_name (str): Name of the network.
        """
        self.client = Client()
        self.container_name = container_name
        self.network_name = network_name

    def apply_profile(
        self,
        profile_name: str = "microk8s.profile",
        target_profile_name: str = "cos-profile",
    ):
        """Apply LXD profile.

        Args:
            profile_name (Optional[str]): Name of the profile to apply.
            target_profile_name (Optional[str]): Name of the target profile.
                Defaults to 'cos-profile'.
        """
        profile_path = Path("tests/integration/data") / profile_name

        with profile_path.open() as file:
            try:
                raw_profile = yaml.safe_load(file)
                config = raw_profile.get("config", {})
                devices = raw_profile.get("devices", {})
                self.client.profiles.create(target_profile_name, config=config, devices=devices)
                log.info("Profile %s applied successfully.", target_profile_name)
            except (yaml.YAMLError, *LXDExceptions):
                log.exception("Failed to read or apply LXD profile")

    def create_container(self, name: str):
        """Create a container.

        Args:
            name (str): Name of the container.

        Returns:
            container: The created container instance, or None if creation fails.
        """
        log.info("Creating container")
        config: Dict[str, Any] = {
            "name": name,
            "source": {
                "type": "image",
                "mode": "pull",
                "server": "https://cloud-images.ubuntu.com/releases",
                "protocol": "simplestreams",
                "alias": "22.04",
            },
            "type": "container",
            "devices": {},
            "profiles": ["default", "cos-profile"],
        }
        if self.network_name:
            config["devices"]["eth0"] = {
                "name": "eth0",
                "nictype": "bridged",
                "parent": self.network_name,
                "type": "nic",
            }
        try:
            container = self.client.instances.create(config, wait=True)
            log.info("Starting Container")
            container.start(wait=True)
            time.sleep(60)
            return container

        except LXDExceptions:
            log.exception("Failed to create or start container")
            return None

    def create_network(
        self,
        network_name: str,
        subnet_cidr: str = "10.10.0.0/24",
        reserved_addresses: int = 5,
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

        try:
            log.info(
                "Creating network '%s' with %s reserved addresses.",
                network_name,
                reserved_addresses,
            )
            self.client.networks.create(**network_config)
            log.info("Network created successfully.")
            reserved_start = dhcp_range_stop + 1
            log.info("Reserved IP range: %s-%s", reserved_start, reserved_stop)
            return reserved_start, reserved_stop
        except LXDExceptions:
            log.exception("Failed to create network")
            raise

    def create_substrate(self) -> str:
        """Create a COS substrate.

        Returns:
            str: The generated kubeconfig.

        Raises:
            RuntimeError: when the container's snapd fails to load seed
        """
        self.apply_profile()
        reserved_start, reserved_stop = self.create_network(self.network_name)
        container = self.create_container(self.container_name)
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

    def delete_container(self, container):
        """Delete a container.

        Args:
            container: Container instance to be deleted.
        """
        try:
            container.stop(wait=True)
            container.delete(wait=True)
            log.info("Container deleted successfully.")
        except LXDExceptions:
            log.error("Failed to delete container")

    def delete_network(self, network_name: str):
        """Delete a network.

        Args:
            network_name (str): Name of the network.
        """
        network = self.client.networks.get(network_name)
        network.delete(wait=True)

    def enable_microk8s_addons(self, container, ranges: str):
        """Enable MicroK8s addons.

        Args:
            container: Container instance.
            ranges (str): MetalLB IP ranges.
        """
        addons = ["dns", "hostpath-storage", f"metallb:{ranges}"]
        for addon in addons:
            self.execute_command(container, ["sudo", "microk8s", "enable", addon])

    def execute_command(self, container, command: List[str]):
        """Execute a command inside a container.

        Args:
            container: Container instance.
            command (list): Command to execute.

        Returns:
            Tuple[int, bytes, bytes]: rc, stdout, and stderr
        """
        log.info("Running command")
        try:
            rc, stdout, stderr = container.execute(command)
            if rc != 0:
                log.error(
                    "Failed to run %s with return code %s. stdout: %s, stderr: %s",
                    command,
                    rc,
                    stdout,
                    stderr,
                )

            return rc, stdout, stderr
        except LXDExceptions:
            log.exception("Failed to execute command")
            return None

    def get_kubeconfig(self, container) -> str:
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
            [
                "sudo",
                "snap",
                "install",
                "microk8s",
                "--channel=1.28/stable",
                "--classic",
            ],
        )

    def remove_profile(self, profile_name: str = "cos-profile"):
        """Remove an LXD profile.

        Args:
            profile_name (Optional[str]): Name of the profile to remove. Defaults to 'cos-profile'.
        """
        try:
            profile = self.client.profiles.get(profile_name)
            profile.delete()
            log.info("Profile %s removed successfully.", profile_name)
        except LXDExceptions:
            log.exception("Failed to remove profile %s", profile_name)

    def teardown_substrate(self):
        """Teardown the COS substrate."""
        container = self.client.instances.get(self.container_name)
        self.delete_container(container)
        self.delete_network(self.network_name)
        self.remove_profile()
