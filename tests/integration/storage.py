# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic test methods for testing kubernetes storage."""

import dataclasses
from pathlib import Path
from typing import Generator, List

from juju import model, unit
from kubernetes.client import ApiClient
from kubernetes.utils import create_from_yaml

from . import helpers


def _get_data_file_path(name) -> str:
    """Retrieve the full path of the specified test data file."""
    path = Path(__file__).parent / "data" / "test_storage_provider" / name
    return str(path)


@dataclasses.dataclass
class StorageProviderManifests:
    """Storage provider manifests.

    Attributes:
        pvc:           PVC manifest file name.
        pv_writer_pod: PV writer pod manifest file name.
        pv_reader_pod: PV reader pod
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
        """Return the number of manifests.

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
        cluster:            The k8s cluster model.
        manifests:          The storage provider manifests.
    """

    name: str
    storage_class_name: str
    provisioner: str
    cluster: model.Model
    manifests: StorageProviderManifests


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
    manifests = definition.manifests
    assert definition.provisioner in stdout, f"No {definition.name} provisioner found in: {stdout}"
    created: List = []

    try:
        # Create PVC.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pvc)))

        # Create a pod that writes to the PV.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pv_writer_pod)))

        # Wait for the pod to exit successfully.
        await helpers.wait_pod_phase(k8s, "pv-writer-test", "Succeeded")

        # Create a pod that reads the PV data and writes it to the log.
        created.extend(*create_from_yaml(api_client, _get_data_file_path(manifests.pv_reader_pod)))

        await helpers.wait_pod_phase(k8s, "pv-reader-test", "Succeeded")

        # Check the logged PV data.
        logs = await helpers.get_pod_logs(k8s, "pv-reader-test")
        assert "PVC test data" in logs
    finally:
        # Cleanup
        for resource in reversed(created):
            kind = resource.kind
            name = resource.metadata.name
            event = await k8s.run(f"k8s kubectl delete {kind} {name}")
            result = await event.wait()
