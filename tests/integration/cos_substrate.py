# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aids in testing COS substrate on LXD."""

import gzip
import ipaddress
import json
import logging
import lzma
import re
import shlex
import subprocess
import time
from io import BytesIO
from pathlib import Path
from platform import freedesktop_os_release as os_release
from typing import Any, Dict, List, Protocol, Tuple, Union
from urllib.request import urlopen

import yaml
from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

log = logging.getLogger(__name__)
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
LXDExceptions = (NotFound, LXDAPIException, ClientConnectionFailed)
K8S_PROFILE_URL = "https://raw.githubusercontent.com/canonical/k8s-snap/refs/heads/main/tests/integration/lxd-profile.yaml"


def _adjust_loopback_device(devices: Dict[str, Any]) -> Dict[str, Any]:
    """Adjust loopback devices."""
    # loopback device configuration for LXD profile based on current
    # system usage, intentionally avoiding conflicts with existing loop devices.

    lsblk = subprocess.check_output(["lsblk"])
    loop_lines = filter(lambda ln: "loop" in ln, lsblk.decode().strip().splitlines())
    in_use = {int(line.split()[0][4:]) for line in loop_lines}
    free = filter(lambda x: x not in in_use, range(2**20))

    replacement = {**devices}
    for device_name, device in devices.items():
        if device_name.startswith("dev-loop") and device.get("type") == "unix-block":
            del replacement[device_name]
            device["minor"] = loop = str(next(free))
            device["path"] = f"/dev/loop{loop}"
            replacement[f"dev-loop{loop}"] = device
    return replacement


def _gzip_to_xz(input_bytes: bytes) -> bytes:
    """Convert gzip bytes to xz compressed bytes."""
    xz_buffer = BytesIO()
    with gzip.GzipFile(fileobj=BytesIO(input_bytes)) as gz:
        with lzma.LZMAFile(xz_buffer, mode="wb") as xz_file:
            decompressed_data = gz.read()
            xz_file.write(decompressed_data)

    return xz_buffer.getvalue()


class COSSubstrate(Protocol):
    """Interface for managing a COS substrate."""

    def create_substrate(self) -> bytes:
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
        self.client = Client(timeout=1200)  # 20 minutes
        self.container_name = container_name
        self.network_name = network_name

    def apply_profile(
        self,
        target_profile_name: str = "cos-profile",
    ):
        """Apply LXD profile.

        Args:
            profile_name (Optional[str]): Name of the profile to apply.
            target_profile_name (Optional[str]): Name of the target profile.
                Defaults to 'cos-profile'.
        """
        with urlopen(K8S_PROFILE_URL) as file:
            try:
                raw_profile = yaml.safe_load(file)
                config = raw_profile.get("config", {})
                devices = _adjust_loopback_device(raw_profile.get("devices", {}))
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
        release = os_release()

        config: Dict[str, Any] = {
            "name": name,
            "source": {
                "type": "image",
                "mode": "pull",
                "server": "https://cloud-images.ubuntu.com/releases",
                "protocol": "simplestreams",
                "alias": release["VERSION_ID"],
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

    def create_substrate(self) -> bytes:
        """Create a COS substrate.

        Returns:
            bytes: The generated kubeconfig.

        Raises:
            RuntimeError: when the container's snapd fails to load seed
        """
        self.apply_profile()
        reserved_start, reserved_stop = self.create_network(self.network_name)
        container = self.create_container(self.container_name)
        max_attempts, sleep_duration = 10, 30
        for _ in range(max_attempts):
            rc, _, _ = self.execute_command(
                container, ["snap", "wait", "system", "seed.loaded"], check=False
            )
            if rc == 0:
                break
            time.sleep(sleep_duration)
        else:
            raise RuntimeError("Failed to wait for system seed")

        self.bootstrap_k8s(container, f"{reserved_start}-{reserved_stop}")
        self.ready_k8s(container)
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

    def bootstrap_k8s(self, container, range: str):
        """Enable MicroK8s addons.

        Args:
            container: Container instance.
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
            self.execute_command(container, shlex.split("sudo " + cmd))

    def ready_k8s(self, container):
        """Wait for K8s to be ready.

        Args:
            container: Container instance.
        """
        max_attempts, sleep_duration = 10, 30
        command = "k8s status --wait-ready"
        for _ in range(max_attempts):
            rc, stdout, stderr = self.execute_command(container, shlex.split(command), check=False)
            if rc == 0 and b"failed" not in stdout.lower():
                break
            log.warning("K8s not ready yet, retrying...")
            log.info("K8s status output: %s", stdout.decode())
            log.info("K8s status errors: %s", stderr.decode())
            time.sleep(sleep_duration)
        else:
            raise RuntimeError("Failed to wait for system seed")

    def inspect_k8s(self, container):
        """Inspect K8s status.

        Args:
            container: Container instance.
        """
        command = "k8s inspect"
        rc, stdout, stderr = self.execute_command(container, shlex.split(command), check=False)
        if rc != 0:
            log.error("K8s inspect failed with rc=%s", rc)
            log.error("K8s inspect stdout: %s", stdout.decode())
            log.error("K8s inspect stderr: %s", stderr.decode())
            return
        artifacts = re.findall(r"(\S+\.tar\.gz)", stdout.decode())
        log.info("K8s inspect artifacts: %s", list(artifacts))
        for artifact in artifacts:
            local = f"juju-crashdump-{self.container_name}-{Path(artifact).stem}.xz"
            log.info("Retrieving artifact: %s to %s", artifact, local)
            with open(local, "wb") as f:
                xz = _gzip_to_xz(container.files.get(artifact))
                f.write(xz)

    def execute_command(self, container, command: List[str], check: bool = True):
        """Execute a command inside a container.

        Args:
            container: Container instance.
            command (list): Command to execute.
            check (bool): Whether to raise an error on non-zero return code.

        Returns:
            Tuple[int, bytes, bytes]: rc, stdout, and stderr
        """
        _cmd = shlex.join(command)
        log.info("Running command: %s", _cmd)
        rc, stdout, stderr = container.execute(command, decode=False)
        if check and rc != 0:
            raise RuntimeError(
                f"Failed to run {_cmd} with {rc=}. {stdout=}, {stderr=}",
            )
        return rc, stdout, stderr

    def get_kubeconfig(self, container) -> bytes:
        """Get kubeconfig from a container.

        Args:
            container: Container instance.

        Returns:
            str: The kubeconfig.
        """
        command = "sudo k8s config"
        _, stdout, _ = self.execute_command(container, shlex.split(command))
        return stdout

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
        self.inspect_k8s(container)
        self.delete_container(container)
        self.delete_network(self.network_name)
        self.remove_profile()
