#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
import re

import pytest
from helpers import ready_nodes, sonobuoy_tar_gz
from juju import model, unit
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle for cncf conformance testing, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
]


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


async def test_cncf_conformance(ops_test: OpsTest, kubernetes_cluster: model.Model):
    """Run CNCF conformance test."""
    k8s: unit.Unit = kubernetes_cluster.applications["k8s"].units[0]

    data = k8s.machine.safe_data
    arch = data["hardware-characteristics"]["arch"]

    sonobuoy_url = await sonobuoy_tar_gz(ops_test, arch)

    action = await k8s.run(
        " && ".join(
            [
                f"curl -L {sonobuoy_url} -o sonobuoy.tar.gz",
                "tar xvzf sonobuoy.tar.gz",
                "./sonobuoy version",
            ]
        )
    )
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to install sonobuoy"

    action = await k8s.run("./sonobuoy run --plugin e2e --wait")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to run e2e plugin"

    action = await k8s.run("./sonobuoy retrieve -f sonobuoy_e2e.tar.gz")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to retrieve e2e results"

    action = await k8s.run("./sonobuoy results sonobuoy_e2e.tar.gz")
    result = await action.wait()
    output = result.results["stdout"]
    log.info(output)
    failed_tests = int(re.search("Failed: (\\d+)", output).group(1))
    assert failed_tests == 0, f"{failed_tests} tests failed"
