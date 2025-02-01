#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import logging
import yaml
import json

import pytest
from pathlib import Path
from typing import List
from juju import model
from kubernetes.utils import create_from_yaml

from . import helpers

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
pytestmark = [
    pytest.mark.bundle(file="test_registries/test-bundle-docker-registry.yaml", apps_local=["k8s"])
]

log = logging.getLogger(__name__)


def _get_data_file_path(name) -> Path:
    """Retrieve the full path of the specified test data file."""
    return Path(__file__).parent / "data" / "test_registries" / name


@pytest.mark.abort_on_fail
async def test_ready_nodes(kubernetes_cluster: model.Model):
    # Check that the units are ready
    k8s_app = kubernetes_cluster.applications["k8s"]

    assert k8s_app


@pytest.mark.abort_on_fail
async def test_custom_registry(kubernetes_cluster: model.Model, api_client):
    """Test that the charm configures the correct directory and can access a custom registry."""
    created: List = []

    # Get the IP of the machine hosting the docker registry charm
    docker_registry_unit = kubernetes_cluster.applications["docker-registry"].units[0]
    docker_registry_ip = await docker_registry_unit.get_public_address()

    # Define the custom registry configuration
    config_string = json.dumps(
        [{"url": f"http://{docker_registry_ip}:5000", "host": f"{docker_registry_ip}:5000"}]
    )

    custom_registry_config = {"containerd-custom-registries": config_string}

    # Apply the custom registry configuration to the k8s charm
    await kubernetes_cluster.applications["k8s"].set_config(custom_registry_config)
    await kubernetes_cluster.wait_for_idle(status="active")

    # Run Docker commands in the Docker registry unit
    action = await docker_registry_unit.run_action("push", image="busybox:latest", pull=True)
    await action.wait()

    # Create a pod that uses the busybox image from the custom registry
    # Image: {docker_registry_ip}:5000/busybox:latest
    test_pod_manifest = list(yaml.safe_load_all(_get_data_file_path("pod.yaml").open()))

    # Add image
    test_pod_manifest[0]["spec"]["containers"][0][
        "image"
    ] = f"{docker_registry_ip}:5000/busybox:latest"

    k8s_unit = kubernetes_cluster.applications["k8s"].units[0]
    try:
        created.extend(*create_from_yaml(api_client, yaml_objects=test_pod_manifest))
        await helpers.wait_pod_phase(k8s_unit, "test-pod", "Succeeded")
    finally:
        # Cleanup
        for resource in reversed(created):
            kind = resource.kind
            name = resource.metadata.name
            event = await k8s_unit.run(f"k8s kubectl delete {kind} {name}")
            result = await event.wait()
