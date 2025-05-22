# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Configuration for kubernetes services.

The format for each file is a set of key-value pairs, where each line contains a key and its value.
"""

from pathlib import Path

from literals import (
    KUBE_APISERVER_ARGS,
    KUBE_CONTROLLER_MANAGER_ARGS,
    KUBE_PROXY_ARGS,
    KUBE_SCHEDULER_ARGS,
    KUBELET_ARGS,
)


class FileArgsConfig:
    """Configuration for file arguments."""

    def __init__(self):
        self.extra_node_kube_apiserver_args = {}
        self.extra_node_kube_controller_manager_args = {}
        self.extra_node_kube_scheduler_args = {}
        self.extra_node_kube_proxy_args = {}
        self.extra_node_kubelet_args = {}
        self._existing = {}
        self._load()

    def _load_file(self, file_path: Path) -> dict[str, str]:
        """Load the arguments from a file.

        Args:
            file_path: the path to the file containing the arguments.

        Returns:
            A dictionary of arguments.
        """
        args = {}
        for line in file_path.read_text().splitlines():
            if line.startswith("--"):
                key, value = line.split("=", 1)
                args[key] = value.strip('"').strip("'")
        return args

    def _save_file(self, file_path: Path, args: dict[str, str]):
        """Save the arguments to a file.

        Args:
            file_path: the path to the file to save the arguments.
            args: a dictionary of arguments to save.
        """
        lines = []
        for key, value in args.items():
            quoted = value.strip("'").strip('"')
            lines.append(f"{key}='{quoted}'\n")
        with file_path.open("w") as f:
            f.write("".join(lines))

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
                self._existing[service] = self._load_file(file_path)

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
            if file_path.exists():
                updated_args = {**self._existing[service], **extra_args}
                self._save_file(file_path, updated_args)
