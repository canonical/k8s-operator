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
from typing import List

import helpers
import jubilant
import pytest
import yaml
from kubernetes.utils import create_from_yaml
from literals import ONE_MIN, TEST_DATA

APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test_registries/test-bundle-docker-registry.yaml", apps_local=APPS),
    pytest.mark.architecture("amd64"),
]

log = logging.getLogger(__name__)

TEST_DATA_PATH = TEST_DATA / "test_registries" / "pod.yaml"
TEST_IMAGE = "busybox:1.36"
TEST_SOURCE_IMAGE = f"rocks.canonical.com/cdk/{TEST_IMAGE}"


def test_custom_registry(k8s_cluster: jubilant.Juju, api_client, timeout: int):
    """Test that the charm configures the correct directory and can access a custom registry."""
    created: List = []

    status = k8s_cluster.status()
    registry_unit = helpers.unit_names(k8s_cluster, "docker-registry")[0]
    registry_ip = status.get_units("docker-registry")[registry_unit].public_address

    config_string = json.dumps(
        [{"url": f"http://{registry_ip}:5000", "host": f"{registry_ip}:5000"}]
    )
    tagged_image = f"{registry_ip}:5000/{TEST_IMAGE}"

    with helpers.fast_forward(k8s_cluster, ONE_MIN):
        k8s_cluster.config("k8s", {"containerd-custom-registries": config_string})
        helpers.wait_active(k8s_cluster, timeout=timeout * 60)

    # juju.run raises TaskError if the action fails.
    k8s_cluster.run(
        registry_unit,
        "push",
        {"image": TEST_SOURCE_IMAGE, "pull": True, "tag": tagged_image},
    )

    # Create a pod that uses the busybox image from the custom registry.
    test_pod_manifest = list(yaml.safe_load_all(TEST_DATA_PATH.read_text()))
    random_pod_name = "test-pod-" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=5)
    )
    test_pod_manifest[0]["metadata"]["name"] = random_pod_name
    test_pod_manifest[0]["spec"]["containers"][0]["image"] = tagged_image

    k8s_unit = helpers.get_leader(k8s_cluster, "k8s")
    try:
        created.extend(*create_from_yaml(api_client, yaml_objects=test_pod_manifest))
        helpers.wait_pod_phase(k8s_cluster, k8s_unit, random_pod_name, "Running")
    finally:
        for resource in created:
            with contextlib.suppress(jubilant.TaskError):
                k8s_cluster.exec(
                    f"k8s kubectl delete {resource.kind} {resource.metadata.name}",
                    unit=k8s_unit,
                )
