# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic test methods for testing kubernetes storage."""

import contextlib
import dataclasses
import logging
from typing import Generator, List

import helpers
import jubilant
from kubernetes.client import ApiClient
from kubernetes.utils import create_from_yaml
from literals import TEST_DATA

log = logging.getLogger(__name__)
STORAGE_PATH = TEST_DATA / "test_storage_provider"


def _get_data_file_path(name: str) -> str:
    """Retrieve the full path of the specified test data file.

    Args:
        name: The manifest file name.

    Returns:
        The absolute path to the manifest file.
    """
    return str(STORAGE_PATH / name)


@dataclasses.dataclass
class StorageProviderManifests:
    """Storage provider manifests.

    Attributes:
        pvc:           PVC manifest file name.
        pv_writer_pod: PV writer pod manifest file name.
        pv_reader_pod: PV reader pod manifest file name.
    """

    pvc: str
    pv_writer_pod: str
    pv_reader_pod: str

    def __iter__(self) -> Generator[str, None, None]:
        """Iterate over the manifest names.

        Yields:
            str: The manifest file name.
        """
        for field in dataclasses.fields(self):
            yield getattr(self, field.name)

    def __reversed__(self) -> Generator[str, None, None]:
        """Iterate over the manifest names in reverse.

        Yields:
            str: The manifest file name.
        """
        for field in reversed(dataclasses.fields(self)):
            yield getattr(self, field.name)


@dataclasses.dataclass
class StorageProviderTestDefinition:
    """Storage provider test definition.

    Attributes:
        name:               Name of the test definition.
        storage_class_name: The name of the storage class.
        provisioner:        The storage class provisioner.
        juju:               Jubilant Juju instance for the cluster's model.
        manifests:          The storage provider manifests.
    """

    name: str
    storage_class_name: str
    provisioner: str
    juju: jubilant.Juju
    manifests: StorageProviderManifests


def exec_storage_class(definition: StorageProviderTestDefinition, api_client: ApiClient) -> None:
    """Test that a storage class is available and validate pv attachments.

    Args:
        definition: The storage provider test definition.
        api_client: The k8s api client.
    """
    juju = definition.juju
    unit = helpers.get_leader(juju, "k8s")
    stdout = juju.exec(
        "k8s kubectl get sc -o=jsonpath='{.items[*].provisioner}'", unit=unit
    ).stdout
    manifests = definition.manifests
    assert definition.provisioner in stdout, f"No {definition.name} provisioner found in: {stdout}"
    created: List = []

    try:
        # Create PVC.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pvc)))

        # Create a pod that writes to the PV.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pv_writer_pod)))

        # Wait for the pod to exit successfully.
        helpers.wait_pod_phase(juju, unit, "pv-writer-test", "Succeeded")

        # Create a pod that reads the PV data and writes it to the log.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pv_reader_pod)))

        helpers.wait_pod_phase(juju, unit, "pv-reader-test", "Succeeded")

        # Check the logged PV data.
        logs = helpers.get_pod_logs(juju, unit, "pv-reader-test")
        assert "PVC test data" in logs
    finally:
        # Cleanup. A delete of an already-gone resource exits non-zero, which juju.exec
        # would raise; suppress that so cleanup of the remaining resources still runs.
        for resource in reversed(created):
            kind = resource.kind
            name = resource.metadata.name
            with contextlib.suppress(jubilant.TaskError):
                juju.exec(f"k8s kubectl delete {kind} {name}", unit=unit)
