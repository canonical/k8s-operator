# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the inspector module."""

import unittest
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

from inspector import ClusterInspector
from lightkube.core.exceptions import ApiError
from lightkube.resources.core_v1 import Node, Pod


class TestClusterInspector(unittest.TestCase):
    """Tests for the ClusterInspector class."""

    def setUp(self):
        """Set up common test fixtures."""
        self.inspector = ClusterInspector(Path("/path/to/kubeconfig"))
        self.mock_client = MagicMock()
        self.inspector.client = self.mock_client

    def test_get_nodes_returns_unready(self):
        """Test that get_nodes returns unready nodes."""
        mock_node1 = MagicMock(spec=Node)
        mock_node1.status = "Ready"
        mock_node1.metadata.name = "node1"

        mock_node2 = MagicMock(spec=Node)
        mock_node2.status = "NotReady"
        mock_node2.metadata.name = "node2"

        self.mock_client.list.return_value = [mock_node1, mock_node2]

        nodes: List[Node] = self.inspector.get_nodes({"role": "control-plane"})

        self.mock_client.list.assert_called_once_with(Node, labels={"role": "control-plane"})
        self.assertEqual(len(nodes), 1)
        # pylint: disable=unsubscriptable-object
        self.assertEqual(nodes[0].metadata.name, "node2")  # type: ignore

    def test_get_nodes_api_error(self):
        """Test get_nodes handles API errors."""
        self.mock_client.list.side_effect = ApiError(response=MagicMock())
        with self.assertRaises(ClusterInspector.ClusterInspectorError):
            self.inspector.get_nodes({"role": "control-plane"})

    def test_verify_pods_running_failed_pods(self):
        """Test verify_pods_running when some pods are not running."""
        mock_pod = MagicMock(spec=Pod)
        mock_pod.status.phase = "Running"
        mock_pod.metadata.name = "pod1"

        mock_pod2 = MagicMock(spec=Pod)
        mock_pod2.status.phase = "Failed"
        mock_pod2.metadata.name = "pod2"

        self.mock_client.list.return_value = [mock_pod, mock_pod2]

        result = self.inspector.verify_pods_running(["kube-system"])

        self.assertEqual(result, "kube-system/pod2")
        self.mock_client.list.assert_called_once_with(Pod, namespace="kube-system")

    def test_verify_pods_running_multiple_namespaces(self):
        """Test verify_pods_running with multiple namespaces."""

        def mock_list_pods(_, namespace):
            """Mock the list method to return pods in different states.

            Args:
                namespace: The namespace to list pods from.

            Returns:
                A list of pods in different states.
            """
            if namespace == "ns1":
                mock_pod = MagicMock(spec=Pod)
                mock_pod.status.phase = "Running"
                mock_pod.metadata.name = "pod1"
                return [mock_pod]
            mock_pod = MagicMock(spec=Pod)
            mock_pod.status.phase = "Failed"
            mock_pod.metadata.name = "pod2"
            return [mock_pod]

        self.mock_client.list.side_effect = mock_list_pods

        result = self.inspector.verify_pods_running(["ns1", "ns2"])

        self.assertEqual(result, "ns2/pod2")
        self.assertEqual(self.mock_client.list.call_count, 2)

    def test_verify_pods_running_api_error(self):
        """Test verify_pods_running handles API errors."""
        self.mock_client.list.side_effect = ApiError(response=MagicMock())

        with self.assertRaises(ClusterInspector.ClusterInspectorError):
            self.inspector.verify_pods_running(["default"])
