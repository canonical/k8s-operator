# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helper for interacting with LXD."""

import logging
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound
from pylxd.models import Instance

log = logging.getLogger(__name__)
TEST_DATA = Path(__file__).parent / "data"
LXDExceptions = (NotFound, LXDAPIException, ClientConnectionFailed)


def _merge_dicts(a: dict, b: dict) -> dict:
    """Recursively merge dict b into dict a."""
    for key, value in b.items():
        if key in a and isinstance(a[key], dict) and isinstance(value, dict):
            a[key] = _merge_dicts(a[key], value)
        else:
            a[key] = value
    return a


def _merge_yaml_files(paths: list[str]):
    merged: Dict[str, Any] = {}
    for path in paths:
        full_path = TEST_DATA / path
        if data := yaml.safe_load(full_path.read_text()):
            merged = _merge_dicts(merged, data)
    return merged


class LXDSubstrate:
    """A LXD substrate."""

    def __init__(self) -> None:
        """Initialize LXDSubstrate instance."""
        self.client = Client()

    def apply_profile(self, profiles: List[str], target_profile_name: str):
        """Apply LXD profile.

        Args:
            profiles (List[str]): Name of the profiles to apply together.
            target_profile_name (str): Name of the target profile.
        """
        raw_profile = _merge_yaml_files(profiles)
        config = raw_profile.get("config", {})
        devices = raw_profile.get("devices", {})
        self.client.profiles.create(target_profile_name, config=config, devices=devices)
        log.info("Profile %s applied successfully.", target_profile_name)

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

    def create_container(self, name: str, network: Optional[str]) -> Optional[Instance]:
        """Create a container.

        Args:
            name (str): Name of the container.
            network (Optional[str]): Name of the network to attach the container to.

        Returns:
            container: The created container instance, or None if creation fails.
        """
        log.info("Creating container: %s", name)
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
        if network:
            config["devices"]["eth0"] = {
                "name": "eth0",
                "nictype": "bridged",
                "parent": network,
                "type": "nic",
            }
        try:
            container = self.client.instances.create(config, wait=True)
            log.info("Starting Container: %s", name)
            container.start(wait=True)
            time.sleep(60)
            return container

        except LXDExceptions:
            log.exception("Failed to create or start container: %s", name)
            return None

    def delete_container(self, container: Instance):
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

    def configure_networks(self, networks: List[str]):
        """Configure LXD networks.

        Args:
            networks (List[str]): List of network configuration files.
        """
        for network in networks:
            config = _merge_yaml_files([network])
            self.apply_networks(config["name"], config)

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
        log.info("Network '%s' created successfully.", name)
        return network

    def delete_network(self, network_name: str):
        """Delete a network.

        Args:
            network_name (str): Name of the network.
        """
        network = self.client.networks.get(network_name)
        network.delete(wait=True)
        log.info("Network '%s' deleted successfully.", network_name)

    def execute_command(self, container: Instance, command: List[str]):
        """Execute a command inside a container.

        Args:
            container: Container instance.
            command (list): Command to execute.

        Returns:
            Tuple[int, bytes, bytes]: rc, stdout, and stderr
        """
        _cmd = shlex.join(command)
        log.info("Running command: %s", _cmd)
        try:
            rc, stdout, stderr = container.execute(command, decode=False)
            if rc != 0:
                log.error(
                    "Failed to run %s with return code %s. stdout: %s, stderr: %s",
                    _cmd,
                    rc,
                    stdout,
                    stderr,
                )
            return rc, stdout, stderr
        except LXDExceptions:
            log.exception("Failed to execute command: %s", _cmd)
            return -1, b"", b""
