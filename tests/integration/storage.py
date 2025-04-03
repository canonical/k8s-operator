# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic test methods for testing kubernetes storage."""

import dataclasses
import logging
from pathlib import Path
from string import Template
from typing import List

import yaml
from juju import model, unit
from kubernetes.client import ApiClient
from kubernetes.utils import create_from_dict, create_from_yaml

from . import helpers

log = logging.getLogger(__name__)

STORAGE_PATH = Path(__file__).parent / "data" / "test_storage_provider"
DYNAMIC_PVC = STORAGE_PATH / "dynamic-pvc.yaml"
PV_WRITER_POD = STORAGE_PATH / "pv-writer-pod.yaml"
PV_READER_POD = STORAGE_PATH / "pv-reader-pod.yaml"


@dataclasses.dataclass
class StorageProviderTestDefinition:
    """Storage provider test definition.

    Attributes:
        name:               Name of the test definition.
        storage_class_name: The name of the storage class.
        provisioner:        The storage class provisioner.
        cluster:            The k8s cluster model.
    """

    name: str
    storage_class_name: str
    provisioner: str
    cluster: model.Model


async def exec_storage_class(definition: StorageProviderTestDefinition, api_client: ApiClient):
    """Test that a storage class is available and validate pv attachments.

    Args:
        definition: The storage provider test definition.
        api_client: The k8s api client.
    """
    k8s: unit.Unit = definition.cluster.applications["k8s"].units[0]
    event = await k8s.run("k8s kubectl get sc -o=jsonpath='{.items[*].provisioner}'")
    result = await event.wait()
    stdout = result.results["stdout"]
    assert definition.provisioner in stdout, f"No {definition.name} provisioner found in: {stdout}"
    created: List = []
    sc_name = definition.storage_class_name

    try:
        with DYNAMIC_PVC.open("r") as pvc_file:
            pvc_template = Template(pvc_file.read())
        pvc_str = pvc_template.substitute(storage_class_name=sc_name)

        # Create PVC.
        log.info("Creating PVC: sc=%s", sc_name)
        created.extend(create_from_dict(api_client, yaml.safe_load(pvc_str)))

        # Create a pod that writes to the PV.
        log.info("Creating PV writer pod: sc=%s", sc_name)
        created.extend(*create_from_yaml(api_client, str(PV_WRITER_POD)))

        # Wait for the pod to exit successfully.
        log.info("Waiting for PV writer pod: sc=%s", sc_name)
        await helpers.wait_pod_phase(k8s, "pv-writer-test", "Succeeded")

        # Create a pod that reads the PV data and writes it to the log.
        log.info("Creating PV reader pod: sc=%s", sc_name)
        created.extend(*create_from_yaml(api_client, str(PV_READER_POD)))
        await helpers.wait_pod_phase(k8s, "pv-reader-test", "Succeeded")

        # Check the logged PV data.
        log.info("Checking logs from reader pod: sc=%s", sc_name)
        logs = await helpers.get_pod_logs(k8s, "pv-reader-test")
        assert "PV test data" in logs, f"PV data not found in logs: {logs}"
    finally:
        # Cleanup
        for resource in reversed(created):
            kind = resource.kind
            name = resource.metadata.name
            event = await k8s.run(f"k8s kubectl delete {kind} {name}")
            result = await event.wait()
