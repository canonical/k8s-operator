#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import json
import logging
import random
import string
from pathlib import Path
from typing import List

import helpers
import pytest
import yaml
from juju import model
from kubernetes.utils import create_from_yaml

pytestmark = [
    pytest.mark.bundle(
        file="test_registries/test-bundle-docker-registry.yaml", apps_local=["k8s"]
    ),
    pytest.mark.architecture("amd64"),
]

log = logging.getLogger(__name__)

TEST_DATA_PATH = Path(__file__).parent / "data" / "test_registries" / "pod.yaml"


@pytest.mark.abort_on_fail
async def test_custom_registry(kubernetes_cluster: model.Model, api_client):
    """Test that the charm configures the correct directory and can access a custom registry."""
    # List of resources created during the test
    created: List = []

    docker_registry_unit = kubernetes_cluster.applications["docker-registry"].units[0]
    docker_registry_ip = await docker_registry_unit.get_public_address()

    config_string = json.dumps(
        [
            {
                "url": f"http://{docker_registry_ip}:5000",
                "host": f"{docker_registry_ip}:5000",
            }
        ]
    )

    custom_registry_config = {"containerd-custom-registries": config_string}

    await kubernetes_cluster.applications["k8s"].set_config(custom_registry_config)
    await kubernetes_cluster.wait_for_idle(status="active")

    action = await docker_registry_unit.run_action("push", image="busybox:latest", pull=True)
    await action.wait()

    # Create a pod that uses the busybox image from the custom registry
    # Image: {docker_registry_ip}:5000/busybox:latest
    test_pod_manifest = list(yaml.safe_load_all(TEST_DATA_PATH.open()))

    random_pod_name = "test-pod-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=5)
    )
    test_pod_manifest[0]["metadata"]["name"] = random_pod_name

    test_pod_manifest[0]["spec"]["containers"][0]["image"] = (
        f"{docker_registry_ip}:5000/busybox:latest"
    )

    k8s_unit = kubernetes_cluster.applications["k8s"].units[0]
    try:
        created.extend(*create_from_yaml(api_client, yaml_objects=test_pod_manifest))
        await helpers.wait_pod_phase(k8s_unit, random_pod_name, "Running")
    finally:
        # Cleanup
        for resource in created:
            kind = resource.kind
            name = resource.metadata.name
            event = await k8s_unit.run(f"k8s kubectl delete {kind} {name}")
            _ = await event.wait()
