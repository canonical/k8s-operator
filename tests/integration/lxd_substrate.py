# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aids in testing COS substrate on LXD."""

import dataclasses
import ipaddress
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.request import urlopen

import yaml
from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound
from pylxd.models import Instance

try:
    from platform import freedesktop_os_release as os_release  # type: ignore[attr-defined]
except ImportError:

    def os_release() -> Dict[str, str]:
        """Fallback os_release for Python < 3.10."""
        release_info = {}
        with open("/etc/os-release") as f:
            for line in f:
                key, _, value = line.partition("=")
                release_info[key] = value.strip().strip('"')
        return release_info


log = logging.getLogger(__name__)
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
LXDExceptions = (NotFound, LXDAPIException, ClientConnectionFailed)
TEST_DATA = Path(__file__).parent / "data"
K8S_PROFILE_URL = "https://raw.githubusercontent.com/canonical/k8s-snap/refs/heads/main/tests/integration/lxd-profile.yaml"


@dataclasses.dataclass
class VMOptions:
    disk_size_gb: int = 32
    memory_size_gb: int = 4
    cpu_count: int = 2
    profile_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    profile_devices: Dict[str, Any] = dataclasses.field(default_factory=dict)


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


def _default_container_profile() -> Dict[str, Any]:
    """Load the default lxd container profile for k8s tuned for this machine."""
    with urlopen(K8S_PROFILE_URL) as file:
        default = yaml.safe_load(file)
        devices = _adjust_loopback_device(default.get("devices", {}))
        return {**default, "devices": devices}


def _merge_dicts(a: dict, b: dict) -> dict:
    """Recursively merge dict b into dict a."""
    for key, value in b.items():
        if key in a and isinstance(a[key], dict) and isinstance(value, dict):
            a[key] = _merge_dicts(a[key], value)
        else:
            a[key] = value
    return a


def _merge_yaml_files(paths: List[str]):
    merged: Dict[str, Any] = {}
    for path in paths:
        full_path = TEST_DATA / path
        if data := yaml.safe_load(full_path.read_text()):
            merged = _merge_dicts(merged, data)
    return merged


class LXDSubstrate:
    """A LXD Substrate."""

    def __init__(self, vm: Optional[VMOptions] = None) -> None:
        """Initialize LXDSubstrate instance.

        Args:
            vm (Optional[VMOptions]): VM options. If provided, a virtual machine
                will be created instead of a instance.
        """
        self.client = Client(timeout=1200)  # 20 minutes
        self.vm_opts = vm
        self.profile_name: Optional[str] = None

    def apply_profile(self, profile_paths: List[str], profile_name: str):
        """Apply LXD profile.

        Args:
            profile_paths (List[str]): Paths to the profile files to apply.
            profile_name (str): Name of the target profile.
        """
        profile = {}
        if self.vm_opts:
            profile = {
                "config": self.vm_opts.profile_config,
                "devices": self.vm_opts.profile_devices,
            }
        else:
            profile = _default_container_profile()

        merged = _merge_dicts(profile, _merge_yaml_files(profile_paths))
        config = merged.get("config", {})
        devices = merged.get("devices", {})
        self.client.profiles.create(profile_name, config=config, devices=devices)
        self.profile_name = profile_name
        log.info("Profile %s applied successfully.", profile_name)

    def remove_profile(self, profile_name: str):
        """Remove an LXD profile.

        Args:
            profile_name (str): Name of the profile to remove.
        """
        try:
            profile = self.client.profiles.get(profile_name)
            profile.delete()
            if self.profile_name == profile_name:
                self.profile_name = None
            log.info("Profile %s removed successfully.", profile_name)
        except LXDExceptions:
            log.exception("Failed to remove profile %s", profile_name)

    def create_instance(self, name: str, network: Optional[str]) -> Optional[Instance]:
        """Create a instance.

        Args:
            name (str): Name of the instance.
            network (Optional[str]): Name of the network to attach the instance to.

        Returns:
            instance: The created instance, or None if creation fails.
        """
        log.info("Creating instance %s", name)
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
            "profiles": [p for p in ("default", self.profile_name) if p],
        }
        if self.vm_opts:
            config["type"] = "virtual-machine"
            config["config"] = {
                "limits.cpu": str(self.vm_opts.cpu_count),
                "limits.memory": f"{self.vm_opts.memory_size_gb * 1024}MiB",
            }
            config["devices"] = {
                "root": {
                    "path": "/",
                    "pool": "default",
                    "size": f"{self.vm_opts.disk_size_gb * 1024}MiB",
                    "type": "disk",
                }
            }
        if network:
            config["devices"]["eth0"] = {
                "name": "eth0",
                "nictype": "bridged",
                "parent": network,
                "type": "nic",
            }
        instance = self.client.instances.create(config, wait=True)
        log.info("Starting Instance %s", name)
        instance.start(wait=True)
        time.sleep(60)
        return instance

    def delete_instance(self, instance: Instance):
        """Delete a instance.

        Args:
            instance: Lxd instance to be deleted.
        """
        try:
            instance.stop(wait=True)
            instance.delete(wait=True)
            log.info("Instance %s deleted successfully.", instance.name)
        except LXDExceptions:
            log.error("Failed to delete instance %s", instance.name)

    def apply_networks(self, name: str, config: Dict[str, Any]):
        """Configure LXD networks.

        Args:
            name (str): Name of the network to configure.
            config (Dict[str, Any]): Configuration options for the network.
        """
        try:
            network = self.client.networks.get(name)
        except NotFound:
            log.warning("Network %s does not exist, creating.", name)
            network = self.client.networks.create(name)

        if (val := config.get("type", "bridge")) and network.type != val:
            network.type = val
        if (val := config.get("description")) and network.description != val:
            network.description = val
        for key, value in config["config"].items():
            if network.config.get(key) == value:
                continue
            if value == "auto" and network.config.get(key) != "none":
                continue
            network.config[key] = value
        if network.dirty:
            network.save()
        log.info("Network '%s' applied successfully.", name)
        return network

    def delete_network(self, network_name: str):
        """Delete a network.

        Args:
            network_name (str): Name of the network.
        """
        network = self.client.networks.get(network_name)
        network.delete(wait=True)
        log.info("Network '%s' deleted successfully.", network_name)

    def execute_command(self, instance, command: List[str], check: bool = True):
        """Execute a command inside a instance.

        Args:
            instance: Container instance.
            command (list): Command to execute.
            check (bool): Whether to raise an error on non-zero return code.

        Returns:
            Tuple[int, bytes, bytes]: rc, stdout, and stderr
        """
        _cmd = shlex.join(command)
        log.info("Running command: %s", _cmd)
        rc, stdout, stderr = instance.execute(command, decode=False)
        if check and rc != 0:
            raise RuntimeError(
                f"Failed to run {_cmd} with {rc=}. {stdout=}, {stderr=}",
            )
        return rc, stdout, stderr
