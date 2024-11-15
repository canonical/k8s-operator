# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the inspector module."""

import unittest
from unittest.mock import MagicMock, patch

from charms.interface_external_cloud_provider import json
from inspector import ClusterInspector


class TestClusterInspector(unittest.TestCase):
    """Tests for the ClusterInspector class."""

    def setUp(self):
        """Set up common test fixtures."""
        self.inspector = ClusterInspector("/path/to/kubeconfig")

    @patch("inspector.time")
    @patch("inspector.run")
    def test_get_nodes_retries_on_failure(self, mock_run, mock_time):
        """Test that get_nodes retries on failure."""
        mock_time.time.side_effect = [0, 1, 2]
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr=b"first failure"),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "test-node",
                                    "labels": {"role": "control-plane"},
                                },
                                "status": {"conditions": [{"type": "Ready"}]},
                            }
                        ]
                    }
                ).encode(),
                stderr=b"",
            ),
        ]

        nodes = self.inspector.get_nodes({"role": "control-plane"})

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "test-node")
        self.assertEqual(mock_run.call_count, 2)

    @patch("inspector.time")
    @patch("inspector.run")
    def test_verify_pods_running_retries_on_failure(self, mock_run, mock_time):
        """Test that verify_pods_running retries on failure."""
        mock_time.time.side_effect = [0, 1, 2]
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr=b"first failure"),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"items": [{"metadata": {"name": "test-pod"}, "status": {"phase": "Failed"}}]}
                ).encode(),
                stderr=b"",
            ),
        ]

        result = self.inspector.verify_pods_running(["default"])

        self.assertEqual(result, "default/test-pod")
        self.assertEqual(mock_run.call_count, 2)

    @patch("inspector.run")
    def test_verify_pods_running_multiple_namespaces(self, mock_run):
        """Test that verify_pods_running can check multiple namespaces."""
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"items": [{"metadata": {"name": "pod1"}, "status": {"phase": "Running"}}]}
                ).encode(),
            ),
            MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"items": [{"metadata": {"name": "pod2"}, "status": {"phase": "Failed"}}]}
                ).encode(),
            ),
        ]

        result = self.inspector.verify_pods_running(["ns1", "ns2"])
        self.assertEqual(result, "ns2/pod2")
        self.assertEqual(mock_run.call_count, 2)

    @patch("inspector.run")
    def test_verify_pods_running_mixed_status(self, mock_run):
        """Test that verify_pods_running returns the failed pod."""
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {
                "items": [
                    {"metadata": {"name": "pod1"}, "status": {"phase": "Running"}},
                    {"metadata": {"name": "pod2"}, "status": {"phase": "Failed"}},
                ]
            }
        ).encode()

        result = self.inspector.verify_pods_running(["default"])
        self.assertEqual(result, "default/pod2")
