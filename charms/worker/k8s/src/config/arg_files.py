# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Configuration for kubernetes services.

The format for each file is a set of key-value pairs, where each line contains a key and its value.
"""

import logging
from hashlib import sha256
from pathlib import Path
from typing import Dict, Optional, Tuple

from literals import (
    KUBE_APISERVER_ARGS,
    KUBE_CONTROLLER_MANAGER_ARGS,
    KUBE_PROXY_ARGS,
    KUBE_SCHEDULER_ARGS,
    KUBELET_ARGS,
)

import charms.operator_libs_linux.v2.snap as snap

log = logging.getLogger(__name__)
FileArgs = Dict[str, Optional[str]]


class FileArgsConfig:
    """Configuration for file arguments."""

    def __init__(self):
        self.extra_node_kube_apiserver_args = {}
        self.extra_node_kube_controller_manager_args = {}
        self.extra_node_kube_scheduler_args = {}
        self.extra_node_kube_proxy_args = {}
        self.extra_node_kubelet_args = {}
        self._service_args = {}
        self._hash = {}
        self._load()

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

    def _file_content(self, args: FileArgs) -> Tuple[str, bytes]:
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

    def _restart_services(self, services: list[str]):
        """Restart the k8s services.

        Args:
            services: the names of the services to restart.
        """
        if services:
            cache = snap.SnapCache()
            cache["k8s"].restart(services)

    def _load(self):
        """Load the arguments for each service from the files."""
        for service, file_path in [
            ("kube-apiserver", KUBE_APISERVER_ARGS),
            ("kube-controller-manager", KUBE_CONTROLLER_MANAGER_ARGS),
            ("kube-scheduler", KUBE_SCHEDULER_ARGS),
            ("kube-proxy", KUBE_PROXY_ARGS),
            ("kubelet", KUBELET_ARGS),
        ]:
            if file_path.exists():
                self._service_args[service], self._hash[service] = self._load_file(file_path)

    def ensure(self):
        """Ensure the arguments of each file."""
        for service, file_path, extra_args in [
            ("kube-apiserver", KUBE_APISERVER_ARGS, self.extra_node_kube_apiserver_args),
            (
                "kube-controller-manager",
                KUBE_CONTROLLER_MANAGER_ARGS,
                self.extra_node_kube_controller_manager_args,
            ),
            ("kube-scheduler", KUBE_SCHEDULER_ARGS, self.extra_node_kube_scheduler_args),
            ("kube-proxy", KUBE_PROXY_ARGS, self.extra_node_kube_proxy_args),
            ("kubelet", KUBELET_ARGS, self.extra_node_kubelet_args),
        ]:
            adjusted_services = []
            if file_path.exists():
                updated_args = {**self._service_args[service], **extra_args}
                content, hash_val = self._file_content(updated_args)
                if hash_val != self._hash[service]:
                    log.info("Restarting '%s' to adjust %s", service, file_path)
                    file_path.write_text(content)
                    adjusted_services += [service]
            self._restart_services(adjusted_services)
