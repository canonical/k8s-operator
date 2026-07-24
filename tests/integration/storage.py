# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic test methods for testing Kubernetes storage providers."""

import contextlib
import dataclasses
import logging
from string import Template
from typing import List

import helpers
import jubilant
import yaml
from kubernetes.client import ApiClient
from kubernetes.utils import create_from_dict, create_from_yaml
from literals import TEST_DATA

log = logging.getLogger(__name__)

STORAGE_PATH = TEST_DATA / "test_storage_provider"
DYNAMIC_PVC = STORAGE_PATH / "dynamic-pvc.yaml"
PV_WRITER_POD = STORAGE_PATH / "pv-writer-pod.yaml"
PV_READER_POD = STORAGE_PATH / "pv-reader-pod.yaml"


@dataclasses.dataclass
class StorageProviderTestDefinition:
    """Storage provider test definition.

    Attributes:
        name: Name of the test definition.
        storage_class_name: The name of the storage class.
        provisioner: The storage class provisioner.
        juju: Jubilant Juju instance for the cluster's model.
    """

    name: str
    storage_class_name: str
    provisioner: str
    juju: jubilant.Juju


def exec_storage_class(definition: StorageProviderTestDefinition, api_client: ApiClient) -> None:
    """Test that a storage class is available, and validate PV attachments.

    Args:
        definition: The storage provider test definition.
        api_client: The Kubernetes API client.
    """
    juju = definition.juju
    unit = helpers.get_leader(juju, "k8s")
    stdout = juju.exec(
        "k8s kubectl get sc -o=jsonpath='{.items[*].provisioner}'", unit=unit
    ).stdout
    assert definition.provisioner in stdout, f"No {definition.name} provisioner found in: {stdout}"

    created: List = []
    sc_name = definition.storage_class_name
    try:
        pvc_template = Template(DYNAMIC_PVC.read_text())
        pvc_str = pvc_template.substitute(storage_class_name=sc_name)

        # Create PVC.
        log.info("Creating PVC: sc=%s", sc_name)
        created.extend(create_from_dict(api_client, yaml.safe_load(pvc_str)))

        # Create a pod that writes to the PV.
        log.info("Creating PV writer pod: sc=%s", sc_name)
        created.extend(*create_from_yaml(api_client, str(PV_WRITER_POD)))

        # Wait for the pod to exit successfully.
        log.info("Waiting for PV writer pod: sc=%s", sc_name)
        helpers.wait_pod_phase(juju, unit, "pv-writer-test", "Succeeded")

        # Create a pod that reads the PV data and writes it to the log.
        log.info("Creating PV reader pod: sc=%s", sc_name)
        created.extend(*create_from_yaml(api_client, str(PV_READER_POD)))
        helpers.wait_pod_phase(juju, unit, "pv-reader-test", "Succeeded")

        # Check the logged PV data.
        log.info("Checking logs from reader pod: sc=%s", sc_name)
        logs = helpers.get_pod_logs(juju, unit, "pv-reader-test")
        assert "PV test data" in logs, f"PV data not found in logs: {logs}"
    finally:
        for resource in reversed(created):
            with contextlib.suppress(jubilant.TaskError):
                juju.exec(
                    f"k8s kubectl delete {resource.kind} {resource.metadata.name}", unit=unit
                )
