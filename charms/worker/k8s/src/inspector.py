# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""A module for inspecting a Kubernetes cluster."""

import logging
from pathlib import Path
from typing import List, Optional

import httpx
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

    def get_nodes(self, labels: Optional[LabelSelector] = None) -> Optional[List[Node]]:
        """Get nodes from the cluster.

        Args:
            labels: A dictionary of labels to filter nodes.

        Returns:
            A list of the failed nodes that match the label selector.

        Raises:
            ClusterInspectorError: If the nodes cannot be retrieved.
        """
        labels = labels or {}
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
        except (ApiError, httpx.HTTPError) as e:
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
                    if is_not_running(pod):
                        failing_pods.append(f"{namespace}/{pod.metadata.name}")  # type: ignore
            if failing_pods:
                return ", ".join(failing_pods)
        except (ApiError, httpx.HTTPError) as e:
            raise ClusterInspector.ClusterInspectorError(f"Failed to get pods: {e}") from e
        return None


def is_not_running(pod: Pod) -> bool:
    """Check if a pod is not running.

    Args:
        pod: The pod to check.

    Returns:
        True if the pod is not running, False otherwise.
    """
    if not (status := pod.status):
        pod_phase: Optional[str] = "Unknown"
        pod_reason: Optional[str] = "Unknown"
    else:
        pod_phase, pod_reason = status.phase, status.reason

    if pod_phase == "Failed":
        # Failed pods are not running -- full stop
        not_running = True
    elif pod_phase == "Succeeded":
        # Exclude Succeeded pods since they have run and done their work
        not_running = False
    elif status and pod_phase == "Running":
        # Any Running phase pod with not ready containers, should be considered not running
        container_statuses = status.initContainerStatuses or []
        container_statuses += status.containerStatuses or []
        not_running = any(not status.ready for status in container_statuses)
    elif pod_phase == "Unknown":
        # Unknown phase pods are not running
        not_running = True
    else:
        # Any other phase (Pending or Unknown) are not running if they aren't evicted
        not_running = pod_reason != "Evicted"

    if not_running and pod.metadata:
        pod_name, pod_ns = pod.metadata.namespace, pod.metadata.name
        log.warning(
            "Pod/%s/%s in phase=%s is not running because of reason=%s",
            pod_ns,
            pod_name,
            pod_phase,
            pod_reason,
        )

    return not_running
