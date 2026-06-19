#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import contextlib
import json
import logging
import random
import string
from pathlib import Path
from typing import List

import helpers
import jubilant
import pytest
import yaml
from helpers import fast_forward
from kubernetes.utils import create_from_yaml
from literals import ONE_MIN

pytestmark = [
    pytest.mark.bundle(
        file="test_registries/test-bundle-docker-registry.yaml", apps_local=["k8s"]
    ),
    pytest.mark.architecture("amd64"),
]

log = logging.getLogger(__name__)

TEST_DATA_PATH = Path(__file__).parent / "data" / "test_registries" / "pod.yaml"
TEST_IMAGE = "busybox:1.36"
TEST_SOURCE_IMAGE = f"rocks.canonical.com/cdk/{TEST_IMAGE}"


@pytest.mark.abort_on_fail
def test_custom_registry(kubernetes_cluster: jubilant.Juju, api_client, timeout: int):
    """Test that the charm configures the correct directory and can access a custom registry."""
    # List of resources created during the test
    created: List = []

    status = kubernetes_cluster.status()
    docker_registry_unit = next(iter(status.get_units("docker-registry")))
    docker_registry_ip = status.get_units("docker-registry")[docker_registry_unit].public_address

    config_string = json.dumps(
        [
            {
                "url": f"http://{docker_registry_ip}:5000",
                "host": f"{docker_registry_ip}:5000",
            }
        ]
    )

    custom_registry_config = {"containerd-custom-registries": config_string}
    tagged_image = f"{docker_registry_ip}:5000/{TEST_IMAGE}"

    with fast_forward(kubernetes_cluster, ONE_MIN):
        kubernetes_cluster.config("k8s", custom_registry_config)
        kubernetes_cluster.wait(jubilant.all_active, timeout=timeout * 60)

    kubernetes_cluster.run(
        docker_registry_unit,
        "push",
        {"image": TEST_SOURCE_IMAGE, "pull": True, "tag": tagged_image},
    )

    # Create a pod that uses the busybox image from the custom registry
    # Image: {docker_registry_ip}:5000/busybox:1.36
    test_pod_manifest = list(yaml.safe_load_all(TEST_DATA_PATH.open()))

    random_pod_name = "test-pod-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=5)
    )
    test_pod_manifest[0]["metadata"]["name"] = random_pod_name
    test_pod_manifest[0]["spec"]["containers"][0]["image"] = tagged_image

    k8s_unit = next(iter(status.get_units("k8s")))
    try:
        created.extend(*create_from_yaml(api_client, yaml_objects=test_pod_manifest))
        helpers.wait_pod_phase(kubernetes_cluster, k8s_unit, random_pod_name, "Running")
    finally:
        # Cleanup
        for resource in created:
            kind = resource.kind
            name = resource.metadata.name
            with contextlib.suppress(jubilant.TaskError):
                kubernetes_cluster.exec(f"k8s kubectl delete {kind} {name}", unit=k8s_unit)
