#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging
import re
import shlex
import subprocess
import sys

import pytest
import pytest_asyncio
from helpers import cloud_arch, get_kubeconfig, ready_nodes
from juju import model
from pytest_operator.plugin import OpsTest
from tags import CONFORMANCE

log = logging.getLogger(__name__)


# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle for cncf conformance testing, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test-bundle.yaml", apps_local=["k8s", "k8s-worker"]),
    pytest.mark.tags(CONFORMANCE),
]


@pytest_asyncio.fixture
async def sonobuoy_url(request, ops_test):
    """Fixture to return the download URL of sonobuoy."""
    arch = await cloud_arch(ops_test)
    if ver := ops_test.request.config.getoption("--sonobuoy-version"):
        return f"https://github.com/vmware-tanzu/sonobuoy/releases/download/{ver}/sonobuoy_{ver[1:]}_linux_{arch}.tar.gz"


@pytest.mark.abort_on_fail
async def test_nodes_ready(kubernetes_cluster: model.Model):
    """Deploy the charm and wait for active/idle status."""
    k8s = kubernetes_cluster.applications["k8s"]
    worker = kubernetes_cluster.applications["k8s-worker"]
    expected_nodes = len(k8s.units) + len(worker.units)
    await ready_nodes(k8s.units[0], expected_nodes)


@pytest.mark.abort_on_fail
async def test_cncf_conformance(
    request, ops_test: OpsTest, sonobuoy_url, kubernetes_cluster: model.Model, timeout: int
):
    """Run CNCF conformance test."""
    await kubernetes_cluster.wait_for_idle(status="active", timeout=timeout * 60)

    module_name = request.module.__name__
    kubeconfig_path = await get_kubeconfig(ops_test, module_name)

    execute_sonobouy_cmds = [
        f"curl -L {sonobuoy_url} -o /tmp/sonobuoy.tar.gz",
        "tar -xvzf /tmp/sonobuoy.tar.gz -C /tmp",
        f"/tmp/sonobuoy run --kubeconfig {kubeconfig_path}"
        f" --plugin e2e --mode certified-conformance --wait",
        f"/tmp/sonobuoy retrieve --kubeconfig {kubeconfig_path} -f sonobuoy_e2e.tar.gz",
        "/tmp/sonobuoy results sonobuoy_e2e.tar.gz",
    ]

    output: str = ""
    for cmd in execute_sonobouy_cmds:
        try:
            log.info("Execute command %s", cmd)
            result = subprocess.run(shlex.split(cmd), check=True, text=True, capture_output=True)
            output = result.stdout.strip()
            log.info(output)
        except subprocess.CalledProcessError as e:
            print(f"Error running command: {cmd}\n{e.stderr or e}")
            sys.exit(1)

    match = re.search(r"Failed: (\d+)", output)
    assert match, f"No reported failures in {output}"
    failed_tests = int(match.group(1))
    assert failed_tests == 0, f"{failed_tests} tests failed"
