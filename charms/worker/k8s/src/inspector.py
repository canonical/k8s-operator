# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""A module for inspecting a Kubernetes cluster."""

import logging
from pathlib import Path
from typing import List, Optional

from lightkube import ApiError, Client, KubeConfig
from lightkube.core.client import LabelSelector
from lightkube.resources.core_v1 import Node, Pod

log = logging.getLogger(__name__)


class ClusterInspector:
    """A helper class for inspecting a Kubernetes cluster."""

    class ClusterInspectorError(Exception):
        """Base exception for ClusterInspector errors."""

    def __init__(
        self,
        kubeconfig_path: Path,
    ):
        """Initialize the ClusterInspector.

        Args:
            kubeconfig_path: The path to the kubeconfig file.
        """
        self.kubeconfig_path = kubeconfig_path
        # NOTE (mateoflorido): The client is set to None to avoid
        # initializing it when the object is created (e.g. during
        # the charm install as we don't have the kubeconfig yet).
        # The client will be initialized when it's needed using the
        # _get_client method.
        self.client: Optional[Client] = None

    def _get_client(self) -> Client:
        """Return the client instance."""
        if self.client is None:
            config = KubeConfig.from_file(str(self.kubeconfig_path))
            self.client = Client(config=config.get())
        return self.client

    def get_nodes(self, labels: LabelSelector) -> Optional[List[Node]]:
        """Get nodes from the cluster.

        Args:
            labels: A dictionary of labels to filter nodes.

        Returns:
            A list of the failed nodes that match the label selector.

        Raises:
            ClusterInspectorError: If the nodes cannot be retrieved.
        """
        client = self._get_client()
        try:

            def is_node_not_ready(node: Node) -> bool:
                """Check if a node is not ready.

                Args:
                    node: The node to check.

                Returns:
                    True if the node is not ready, False otherwise.
                """
                if not node.status or not node.status.conditions:
                    return True
                return any(
                    condition.type == "Ready" and condition.status != "True"
                    for condition in node.status.conditions
                )

            return [node for node in client.list(Node, labels=labels) if is_node_not_ready(node)]
        except ApiError as e:
            raise ClusterInspector.ClusterInspectorError(f"Failed to get nodes: {e}") from e

    def verify_pods_running(self, namespaces: List[str]) -> Optional[str]:
        """Verify that all pods in the specified namespaces are running.

        Args:
            namespaces: A list of namespaces to check.

        Returns:
            None if all pods are running, otherwise returns a string
            containing the namespaces that have pods not running.

        Raises:
            ClusterInspectorError: If the pods cannot be retrieved.
        """
        client = self._get_client()

        failing_pods = []
        try:
            for namespace in namespaces:
                for pod in client.list(Pod, namespace=namespace):
                    if pod.status.phase != "Running":  # type: ignore
                        failing_pods.append(f"{namespace}/{pod.metadata.name}")  # type: ignore
            if failing_pods:
                return ", ".join(failing_pods)
        except ApiError as e:
            raise ClusterInspector.ClusterInspectorError(f"Failed to get pods: {e}") from e
        return None
