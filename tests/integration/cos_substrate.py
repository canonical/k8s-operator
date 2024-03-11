# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import ipaddress
import logging
import time
from pathlib import Path
from typing import List, Protocol

import yaml
from pylxd import Client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


class COSSubstrate(Protocol):
    """Interface for managing a COS substrate."""

    def create_substrate(self) -> str:
        """Create a COS substrate.

        Returns:
            str: The generated kubeconfig.
        """
        ...

    def teardown_substrate(self):
        """Teardown the COS substrate."""
        ...


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
        self, profile_name: str = "microk8s.profile", target_profile_name: str = "cos-profile"
    ):
        """Apply LXD profile.

        Args:
            profile_name (Optional[str]): Name of the profile to apply.
            target_profile_name (Optional[str]): Name of the target profile. Defaults to 'cos-profile'.
        """
        profile_path = Path("tests/integration/data") / profile_name
        with open(profile_path) as file:
            try:
                raw_profile = yaml.safe_load(file)
                config = raw_profile.get("config", {})
                devices = raw_profile.get("devices", {})
                self.client.profiles.create(target_profile_name, config=config, devices=devices)
                log.info(f"Profile {target_profile_name} applied successfully.")
            except (yaml.YAMLError, Exception) as e:
                log.error(f"Failed to read or apply LXD profile: {e}")

    def create_container(self, name: str):
        """Create a container.

        Args:
            name (str): Name of the container.

        Returns:
            container: The created container instance, or None if creation fails.
        """
        log.info("Creating container")
        config = {
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

        except Exception as e:
            log.error(f"Failed to create or start container: {e}")
            return None

    def create_network(
        self, network_name: str, subnet_cidr: str = "10.10.0.0/24", reserved_addresses: int = 5
    ):
        """Create a network.

        Args:
            network_name (str): Name of the network.
            subnet_cidr (Optional[str]): CIDR of the subnet. Defaults to '10.10.0.0/24'.
            reserved_addresses (Optional[int]): Number of reserved IP addresses. Defaults to 5.
        """
        existing_networks = self.client.networks.all()
        if any(net.name == network_name for net in existing_networks):
            log.info("Network already exists. Skipping creation.")
            return

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
                f"Creating network '{network_name}' with {reserved_addresses} reserved addresses."
            )
            self.client.networks.create(**network_config)
            log.info("Network created successfully.")
            reserved_start = dhcp_range_stop + 1
            return (reserved_start, reserved_stop)
        except Exception as e:
            log.error(f"Failed to create network: {e}")

    def create_substrate(self) -> str:
        """Create a COS substrate.

        Returns:
            str: The generated kubeconfig.
        """
        self.apply_profile()
        reserved_start, reserved_stop = self.create_network(self.network_name)
        container = self.create_container(self.container_name)
        MAX_ATTEMPTS = 10
        SLEEP_DURATION = 30
        for _ in range(MAX_ATTEMPTS):
            rc, _, _ = self.execute_command(container, ["snap", "wait", "system", "seed.loaded"])
            if rc == 0:
                break
            time.sleep(SLEEP_DURATION)
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
        except Exception as e:
            log.error(f"Failed to delete container: {e}")

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
            rc, stdout, stderr = self.execute_command(
                container, ["sudo", "microk8s", "enable", addon]
            )
            if rc != 0:
                log.error(f"Failed to enable {addon}: {stdout}, {stderr}")

    def execute_command(self, container, command: List[str]):
        """Execute a command inside a container.

        Args:
            container: Container instance.
            command (list): Command to execute.
        """
        log.info("Running command")
        try:
            rc, stdout, stderr = container.execute(command)
            if rc != 0:
                log.error(
                    f"Failed to run {command} with return code {rc}. stdout: {stdout}, stderr: {stderr}"
                )

            return rc, stdout, stderr
        except Exception as e:
            log.error(f"Failed to execute command: {e}")

    def get_kubeconfig(self, container) -> str:
        """Get kubeconfig from a container.

        Args:
            container: Container instance.

        Returns:
            str: The kubeconfig.
        """
        rc, stdout, stderr = self.execute_command(container, ["microk8s", "config"])
        if rc != 0:
            log.error(f"Failed to get kubeconfig: {stdout}, {stderr}")
        return stdout

    def install_k8s(self, container):
        """Install Kubernetes inside a container.

        Args:
            container: Container instance.
        """

        return self.execute_command(
            container,
            ["sudo", "snap", "install", "microk8s", "--channel=1.28/stable", "--classic"],
        )

    def remove_profile(self, profile_name: str = "cos-profile"):
        """Remove an LXD profile.

        Args:
            profile_name (Optional[str]): Name of the profile to remove. Defaults to 'cos-profile'.
        """
        try:
            profile = self.client.profiles.get(profile_name)
            profile.delete()
            log.info(f"Profile {profile_name} removed successfully.")
        except Exception as e:
            log.error(f"Failed to remove profile {profile_name}: {e}")

    def teardown_substrate(self):
        """Teardown the COS substrate."""
        container = self.client.instances.get(self.container_name)
        self.delete_container(container)
        self.delete_network(self.network_name)
        self.remove_profile()
