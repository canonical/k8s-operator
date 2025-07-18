# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Configuration for kubernetes services.

The format for each file is a set of key-value pairs, where each line contains a key and its value.
"""

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from literals import (
    CHARM_SYSD_ARGS_FILE,
    KUBE_APISERVER_ARGS_PATH,
    KUBE_APISERVER_SYSD_PATH,
    KUBE_CONTROLLER_MANAGER_ARGS_PATH,
    KUBE_CONTROLLER_MANAGER_SYSD_PATH,
    KUBE_PROXY_ARGS_PATH,
    KUBE_PROXY_SYSD_PATH,
    KUBE_SCHEDULER_ARGS_PATH,
    KUBE_SCHEDULER_SYSD_PATH,
    KUBELET_ARGS_PATH,
    KUBELET_SYSD_PATH,
    SNAP_NAME,
    SNAP_SYSD_ARGS_FILE,
)

import charms.operator_libs_linux.v2.snap as snap

log = logging.getLogger(__name__)
FileArgs = Dict[str, Optional[str]]


@dataclass
class ServiceConfig:
    """Configuration for a single Kubernetes service."""

    name: str
    args_path: Path
    systemd_args_path: Path
    extra_args: FileArgs


class FileArgsConfig:
    """Configuration for file arguments."""

    def __init__(self):
        self.extra_node_kube_apiserver_args = {}
        self.extra_node_kube_controller_manager_args = {}
        self.extra_node_kube_scheduler_args = {}
        self.extra_node_kube_proxy_args = {}
        self.extra_node_kubelet_args = {}

        self._service_args: Dict[str, FileArgs] = {}
        self._file_hashes: Dict[str, bytes] = {}
        self._load()

    def _get_service_configs(self) -> List[ServiceConfig]:
        """Get the k8s service configurations."""
        return [
            ServiceConfig(
                name="kube-apiserver",
                args_path=KUBE_APISERVER_ARGS_PATH,
                systemd_args_path=KUBE_APISERVER_SYSD_PATH / SNAP_SYSD_ARGS_FILE,
                extra_args=self.extra_node_kube_apiserver_args,
            ),
            ServiceConfig(
                name="kube-controller-manager",
                args_path=KUBE_CONTROLLER_MANAGER_ARGS_PATH,
                systemd_args_path=KUBE_CONTROLLER_MANAGER_SYSD_PATH / SNAP_SYSD_ARGS_FILE,
                extra_args=self.extra_node_kube_controller_manager_args,
            ),
            ServiceConfig(
                name="kube-scheduler",
                args_path=KUBE_SCHEDULER_ARGS_PATH,
                systemd_args_path=KUBE_SCHEDULER_SYSD_PATH / SNAP_SYSD_ARGS_FILE,
                extra_args=self.extra_node_kube_scheduler_args,
            ),
            ServiceConfig(
                name="kube-proxy",
                args_path=KUBE_PROXY_ARGS_PATH,
                systemd_args_path=KUBE_PROXY_SYSD_PATH / SNAP_SYSD_ARGS_FILE,
                extra_args=self.extra_node_kube_proxy_args,
            ),
            ServiceConfig(
                name="kubelet",
                args_path=KUBELET_ARGS_PATH,
                systemd_args_path=KUBELET_SYSD_PATH / SNAP_SYSD_ARGS_FILE,
                extra_args=self.extra_node_kubelet_args,
            ),
        ]

    def _get_effective_args_path(self, service: ServiceConfig) -> Path:
        """Get the effective arguments file path for a service."""
        if service.systemd_args_path.exists():
            return service.systemd_args_path.parent / CHARM_SYSD_ARGS_FILE
        return service.args_path

    def _load_file(self, file_path: Path) -> Tuple[FileArgs, bytes]:
        """Load the arguments from a file.

        Args:
            file_path: the path to the file containing the arguments.

        Returns:
            A dictionary of arguments and the hash of the file content.
        """
        args: FileArgs = {}
        contents = file_path.read_text()
        hash_val = sha256(contents.encode()).digest()
        for line in contents.splitlines():
            if line.startswith("--"):
                key, value = line.split("=", 1)
                args[key] = value.strip('"').strip("'")
        return args, hash_val

    def _generate_file_content(self, args: FileArgs) -> Tuple[str, bytes]:
        """Generate the content for the file and its hash.

        Args:
            args: a dictionary of arguments to save.

        Returns:
            The content to save in the file and the hash of the content.
        """
        lines = []
        # Sort the arguments to ensure consistent ordering
        for key, value in sorted(args.items()):
            # Drop argument values that may be None
            if value is not None:
                quoted = value.strip("'").strip('"')
                lines.append(f'{key}="{quoted}"\n')
        content = "".join(lines)
        hash_val = sha256(content.encode()).digest()
        return content, hash_val

    def _restart_services(self, services: List[str]):
        """Restart the k8s services.

        Args:
            services: the names of the services to restart.
        """
        if services:
            cache = snap.SnapCache()
            log.info("Restarting services: %s", ", ".join(services))
            cache[SNAP_NAME].restart(services)

    def _load(self):
        """Load the arguments for each service from the files."""
        for service in self._get_service_configs():
            file_path = self._get_effective_args_path(service)
            if file_path.exists():
                args, file_hash = self._load_file(file_path)
                self._service_args[service.name] = args
                self._file_hashes[service.name] = file_hash

    def ensure(self):
        """Ensure the arguments of each file."""
        adjusted_services: List[str] = []
        for service in self._get_service_configs():
            file_path = self._get_effective_args_path(service)
            if not file_path.exists():
                log.debug("Skipping non-existent arguments file: %s", file_path)
                continue

            updated_args = {**self._service_args[service.name], **service.extra_args}
            content, hash_val = self._generate_file_content(updated_args)
            if hash_val != self._file_hashes[service.name]:
                log.info("Updating arguments for %s at %s", service.name, file_path)
                file_path.write_text(content)
                adjusted_services.append(service.name)
        self._restart_services(adjusted_services)
