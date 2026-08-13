#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code
"""Integration tests."""

import logging
import re

import jubilant
import pytest
import storage
from helpers import fast_forward, wait_active
from kubernetes.client import ApiClient, CoreV1Api, StorageV1Api
from kubernetes.client import models as k8s_models
from literals import ONE_MIN
from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_fixed

# This pytest mark configures the test environment to use the Canonical Kubernetes
# bundle with ceph, for all the test within this module.
APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test-bundle-ceph.yaml", apps_local=APPS),
    pytest.mark.architecture("amd64"),
]
CEPH_CSI_MISSING_NS = re.compile(r"Missing namespace '(\S+)'")
RBD_PLUGIN_SELECTOR = "app=csi-rbdplugin"
log = logging.getLogger(__name__)


def _load_rbd_module(k8s_cluster: jubilant.Juju, v1: CoreV1Api) -> None:
    """Load the rbd kernel module on the k8s machines and restart the node plugin.

    ceph-csi's node plugin runs "modprobe rbd" from inside its own container. Its kmod is
    too old to decompress the zstd-compressed modules that 24.04 ships, so it hands the
    still-compressed file to the kernel, which -- locked down by the LXD VM's secure boot
    -- rejects it as unsigned:

        modprobe: ERROR: could not insert 'rbd': Key was rejected by service

    The container then exits and crash-loops, so rbd.csi.ceph.com is never registered
    with kubelet and every RBD volume fails to mount. Loading the module from the host,
    whose modprobe does understand zstd, turns the container's modprobe into a no-op.
    The plugin pods are deleted afterwards so they restart without waiting out the
    CrashLoopBackOff they have already accumulated.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        v1: The Kubernetes core API.
    """
    status = k8s_cluster.status()
    for app in APPS:
        for unit in status.get_units(app):
            log.info("Loading the rbd kernel module on %s", unit)
            k8s_cluster.exec("modprobe rbd", unit=unit)

    for pod in v1.list_pod_for_all_namespaces(label_selector=RBD_PLUGIN_SELECTOR).items:
        v1.delete_namespaced_pod(pod.metadata.name, pod.metadata.namespace)

    for attempt in Retrying(
        reraise=True,
        stop=stop_after_attempt(30),
        wait=wait_fixed(10),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            pods = v1.list_pod_for_all_namespaces(label_selector=RBD_PLUGIN_SELECTOR).items
            assert pods, "The csi-rbdplugin pods have not been recreated yet"
            for pod in pods:
                ready = [c for c in pod.status.container_statuses or [] if c.ready]
                assert len(ready) == len(pod.spec.containers), (
                    f"Pod {pod.metadata.name} is not ready yet"
                )


@pytest.fixture(scope="module")
def ready_csi_apps(k8s_cluster: jubilant.Juju, api_client: ApiClient, timeout: int) -> None:
    """Wait for the CSI apps to be ready.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        api_client: The Kubernetes API client.
        timeout: Timeout in minutes.
    """
    v1 = CoreV1Api(api_client)
    status = k8s_cluster.status()
    csi_apps = [name for name, app in status.apps.items() if app.charm_name == "ceph-csi"]

    for app in csi_apps:
        # ceph-csi is subordinate, so its units live under the principal's units.
        for unit in status.get_units(app).values():
            if match := CEPH_CSI_MISSING_NS.match(unit.workload_status.message):
                namespace = match.group(1)
                v1.create_namespace(
                    body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
                )
                break

    _load_rbd_module(k8s_cluster, v1)

    with fast_forward(k8s_cluster, ONE_MIN):
        wait_active(k8s_cluster, *csi_apps, timeout=timeout * 60)


@pytest.mark.usefixtures("ready_csi_apps")
def test_ceph_sc(k8s_cluster: jubilant.Juju, api_client: ApiClient):
    """Test that a ceph storage class is available and validate pv attachments."""
    v1 = StorageV1Api(api_client)
    classes = v1.list_storage_class()
    ceph_classes = [sc for sc in classes.items if "ceph" in sc.metadata.name]
    assert len(ceph_classes) > 0, "No ceph storage classes found"
    for sc in ceph_classes:
        definition = storage.StorageProviderTestDefinition(
            "ceph", sc.metadata.name, sc.provisioner, k8s_cluster
        )
        storage.exec_storage_class(definition, api_client)
