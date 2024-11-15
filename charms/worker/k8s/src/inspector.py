"""A module for inspecting a Kubernetes cluster."""

import json
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from subprocess import run
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)
RUN_RETRIES = 180


@dataclass
class Node:
    """A dataclass for representing a Kubernetes node.

    Attributes:
        name: The name of the node.
        status: The status of the node.
        roles: The roles of the node.
        labels: The labels of the node.
    """

    name: str
    status: str
    roles: List[str]
    labels: Dict[str, str]


class ClusterInspector:
    """A helper class for inspecting a Kubernetes cluster."""

    class ClusterInspectorError(Exception):
        """Base exception for ClusterInspector errors."""

    def __init__(
        self,
        kubeconfig_path: Path,
        kubectl: Path = Path("/snap/bin/kubectl"),
    ):
        """Initialize the ClusterInspector.

        Args:
            kubeconfig_path: The path to the kubeconfig file.
            kubectl: The path to the kubectl binary.
        """
        self.kubeconfig_path = kubeconfig_path
        self.kubectl_path = kubectl

    @staticmethod
    def _retried_call(
        cmd: List[str], retry_msg: str, timeout: Optional[int] = None
    ) -> Tuple[bytes, bytes]:
        """Run a command with retries until it succeeds or the timeout is reached.

        Args:
            cmd: The command to run.
            retry_msg: The message to log when retrying.
            timeout: The maximum time to wait for the command to succeed.

        Returns:
            A tuple containing the stdout and stderr from the command.

        Raises:
            ClusterInspectorError: If the command fails after the timeout.
        """
        timeout = RUN_RETRIES if timeout is None else timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            rc = run(cmd, capture_output=True, check=False)
            if rc.returncode == 0:
                return rc.stdout, rc.stderr
            log.error("%s: %s", retry_msg, rc.stderr.decode())
            time.sleep(1)
        raise ClusterInspector.ClusterInspectorError(
            f"Failed to run {cmd} after {timeout} seconds."
        )

    def _kubectl(self, command: str) -> str:
        """Return the kubectl command with the kubeconfig path and command.

        Args:
            command: The kubectl command to run.
        """
        if not self.kubectl_path.exists():
            retry_msg = "Failed to find kubectl. Will retry."
            stdout, _ = self._retried_call(["which", "kubectl"], retry_msg)
            self.kubectl_path = Path(stdout.decode().strip())

        base = f"{self.kubectl_path} --kubeconfig={self.kubeconfig_path}"
        return base + " " + command

    def _parse_nodes(self, raw_nodes: Dict) -> List[Node]:
        """Parse the raw nodes JSON output from kubectl.

        Args:
            raw_nodes: The raw JSON output from kubectl.
        """
        nodes = []
        for node in raw_nodes["items"]:
            try:
                name = node["metadata"]["name"]
                status = node["status"]["conditions"][-1]["type"]
                roles = node["metadata"]["labels"]
                labels = node["metadata"]["labels"]
                nodes.append(Node(name, status, roles, labels))
            except KeyError:
                log.error("Failed to parse node: %s", node)
        return nodes

    def get_nodes(self, label_selector: Dict[str, str]) -> Optional[List[Node]]:
        """Get nodes from the cluster.

        Args:
            label_selector: A dictionary of labels to filter nodes.

        Returns:
            A list of Node objects.

        Raises:
            ClusterInspectorError: If the kubectl command fails or the JSON
                output cannot be parsed.
        """
        cmd = "get nodes -o json"
        if label_selector:
            cmd += " -l "
            cmd += ",".join([f"{k}={v}" for k, v in label_selector.items()])
        cmd = self._kubectl(cmd)
        nodes_json, _ = self._retried_call(
            shlex.split(cmd), "Failed to get nodes. Will retry.", 60
        )
        try:
            raw_nodes = json.loads(nodes_json)
            return self._parse_nodes(raw_nodes)
        except json.JSONDecodeError as e:
            raise ClusterInspector.ClusterInspectorError(
                f"Failed to decode kubectl response: {nodes_json.decode()}"
            ) from e

    def verify_pods_running(
        self, namespaces: List[str], timeout: Optional[int] = None
    ) -> Optional[str]:
        """Verify that all pods in the specified namespaces are running.

        Args:
            namespaces: A list of namespaces to check.
            timeout: The maximum time to wait for the pods to be ready.

        Returns:
            None if all pods are running, otherwise returns a string
            containing the namespaces that have pods not running.

        Raises:
            ClusterInspectorError: If the kubectl command fails or the JSON
                output cannot be parsed.
        """
        failing_pods = []
        for namespace in namespaces:
            cmd = f"get pods -n {namespace} -o json"
            cmd = self._kubectl(cmd)
            pods_json, _ = self._retried_call(
                shlex.split(cmd), f"Failed to get pods in {namespace}. Will retry.", timeout
            )
            try:
                pods = json.loads(pods_json)
            except json.JSONDecodeError as e:
                raise ClusterInspector.ClusterInspectorError(
                    f"Failed to decode kubectl response: {pods_json.decode()}"
                ) from e

            for pod in pods.get("items", []):
                pod_name = pod.get("metadata", {}).get("name", "unknown")
                pod_status = pod.get("status", {})
                phase = pod_status.get("phase", "")

                if phase != "Running":
                    failing_pods.append(f"{namespace}/{pod_name}")
        if failing_pods:
            return "\n".join(failing_pods)
        return None
