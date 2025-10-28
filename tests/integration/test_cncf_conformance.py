#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
import re

import pytest
from helpers import get_kubeconfig, ready_nodes, run_command, sonobuoy_tar_gz
from juju import model, unit
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle for cncf conformance testing, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
    pytest.mark.run_with_k,
]


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


@pytest.mark.abort_on_fail
async def test_cncf_conformance(
    request, ops_test: OpsTest, kubernetes_cluster: model.Model, timeout: int
):
    """Run CNCF conformance test."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]
    await kubernetes_cluster.wait_for_idle(status="active", timeout=timeout * 60)

    data = k8s.machine.safe_data
    arch = data["hardware-characteristics"]["arch"]
    sonobuoy_url = await sonobuoy_tar_gz(ops_test, arch)

    module_name = request.module.__name__
    kubeconfig_path = await get_kubeconfig(ops_test, module_name)

    run_command(
        [
            "curl",
            "-L",
            f"{sonobuoy_url}",
            "-o",
            "/tmp/sonobuoy.tar.gz",
        ]
    )
    run_command(["tar", "-xvzf", "/tmp/sonobuoy.tar.gz", "-C", "/tmp"])

    run_command(
        [
            "/tmp/sonobuoy",
            "run",
            "--kubeconfig",
            f"{kubeconfig_path}",
            "--plugin",
            "e2e",
            "--wait",
        ]
    )

    run_command(
        [
            "/tmp/sonobuoy",
            "retrieve",
            "--kubeconfig",
            f"{kubeconfig_path}",
            "-f",
            "sonobuoy_e2e.tar.gz",
        ]
    )

    output = run_command(["/tmp/sonobuoy", "results", "sonobuoy_e2e.tar.gz"], capture_output=True)
    log.info(output)
    match = re.search("Failed: (\\d+)", str(output))
    failed_tests = int(match.group(1)) if match else 1
    assert failed_tests == 0, f"{failed_tests} tests failed"
