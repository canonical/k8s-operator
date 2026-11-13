# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Regression tests for authoritative Kubernetes Node presence checks."""

import subprocess
from pathlib import Path
from unittest import mock

import k8s.node
import pytest


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("worker-0", k8s.node.Presence.AVAILABLE),
        ("", k8s.node.Presence.UNAVAILABLE),
        ("another-node", k8s.node.Presence.UNRESPONSIVE),
    ],
)
def test_node_presence_requires_a_successful_lookup(response, expected):
    """Only a successful lookup with no matching object confirms Node absence."""
    with mock.patch("k8s.node.kubectl", return_value=response):
        assert k8s.node.present(Path("/kubeconfig"), "worker-0") == expected


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "kubectl", output="kubeconfig not found"),
        subprocess.CalledProcessError(1, "kubectl", output="Forbidden"),
        subprocess.CalledProcessError(1, "kubectl", output=None),
        subprocess.TimeoutExpired("kubectl", 30),
        FileNotFoundError("kubectl not found"),
    ],
)
def test_failed_node_query_is_not_proof_of_absence(error):
    """Missing tools, credentials, and failed API requests must fail closed."""
    with mock.patch("k8s.node.kubectl", side_effect=error):
        assert k8s.node.present(Path("/kubeconfig"), "worker-0") == k8s.node.Presence.UNRESPONSIVE
