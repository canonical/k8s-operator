# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests cloud-integration module."""

from pathlib import Path
from unittest import mock

import ops
import ops.testing
import pytest
from charm import K8sCharm
from ops.interface_aws.requires import AWSIntegrationRequires
from ops.interface_azure.requires import AzureIntegrationRequires
from ops.interface_gcp.requires import GCPIntegrationRequires

TEST_CLUSTER_NAME = "my-cluster"


@pytest.fixture(autouse=True)
def vendor_name():
    """Mock the ExternalCloudProvider name property."""
    with mock.patch(
        "charms.interface_external_cloud_provider.ExternalCloudProvider.name",
        new_callable=mock.PropertyMock,
    ) as mock_vendor_name:
        yield mock_vendor_name


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    meta = Path(__file__).parent / "../../charmcraft.yaml"
    if request.param == "worker":
        meta = Path(__file__).parent / "../../../charmcraft.yaml"
    harness = ops.testing.Harness(K8sCharm, meta=meta.read_text())
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    with mock.patch.object(harness.charm, "get_cloud_name"):
        with mock.patch.object(harness.charm.reconciler, "reconcile"):
            yield harness
    harness.cleanup()


@pytest.mark.parametrize(
    "cloud_name, cloud_relation",
    [
        ("aws", "aws"),
        ("gce", "gcp"),
        ("azure", "azure"),
        ("unknown", None),
    ],
    ids=["aws", "gce", "azure", "unknown"],
)
def test_cloud_detection(harness, cloud_name, cloud_relation):
    """Test that the cloud property returns the correct integration requires object.

    Args:
        harness (ops.testing.Harness): The test harness
        cloud_name (str): The name of the cloud
        cloud_relation (str): The name of the relation
        vendor_name (mock.PropertyMock): The mock for the ExternalCloudProvider name property
    """
    harness.charm.get_cloud_name.return_value = cloud_name
    integration = harness.charm.cloud_integration
    assert integration.cloud is None
    if cloud_name != "unknown":
        harness.add_relation(cloud_relation, "cloud-integrator")
        assert integration.cloud


def test_cloud_aws(harness):
    """Test that the cloud property returns the correct integration requires object.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    harness.charm.get_cloud_name.return_value = "aws"
    with mock.patch(
        "cloud_integration.CloudIntegration.cloud",
        new_callable=mock.PropertyMock,
        return_value=mock.create_autospec(AWSIntegrationRequires),
    ) as mock_property:
        mock_cloud = mock_property()
        mock_cloud.evaluate_relation.return_value = None
        event = mock.MagicMock()
        harness.charm.cloud_integration.integrate(TEST_CLUSTER_NAME, event)
        if harness.charm.is_worker:
            mock_cloud.tag_instance.assert_called_once_with(
                {"kubernetes.io/cluster/my-cluster": "owned"}
            )
        else:
            mock_cloud.tag_instance.assert_called_once_with(
                {
                    "kubernetes.io/cluster/my-cluster": "owned",
                    "k8s.io/role/master": "true",  # wokeignore:rule=master
                }
            )
        mock_cloud.tag_instance_security_group.assert_called_once_with(
            {"kubernetes.io/cluster/my-cluster": "owned"}
        )
        mock_cloud.tag_instance_subnet.assert_called_once_with(
            {"kubernetes.io/cluster/my-cluster": "owned"}
        )
        mock_cloud.enable_object_storage_management.assert_called_once_with(["kubernetes-*"])
        if harness.charm.is_worker:
            mock_cloud.enable_load_balancer_management.assert_not_called()
            mock_cloud.enable_autoscaling_readonly.assert_not_called()
            mock_cloud.enable_instance_modification.assert_not_called()
            mock_cloud.enable_region_readonly.assert_not_called()
            mock_cloud.enable_network_management.assert_not_called()
            mock_cloud.enable_block_storage_management.assert_not_called()
        else:
            mock_cloud.enable_load_balancer_management.assert_called_once()
            mock_cloud.enable_autoscaling_readonly.assert_called_once()
            mock_cloud.enable_instance_modification.assert_called_once()
            mock_cloud.enable_region_readonly.assert_called_once()
            mock_cloud.enable_network_management.assert_called_once()
            mock_cloud.enable_block_storage_management.assert_called_once()
        mock_cloud.enable_instance_inspection.assert_called_once()
        mock_cloud.enable_dns_management.assert_called_once()
        mock_cloud.evaluate_relation.assert_called_once_with(event)


def test_cloud_gce(harness):
    """Test that the cloud property returns the correct integration requires object.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    harness.charm.get_cloud_name.return_value = "gce"
    with mock.patch(
        "cloud_integration.CloudIntegration.cloud",
        new_callable=mock.PropertyMock,
        return_value=mock.create_autospec(GCPIntegrationRequires),
    ) as mock_property:
        mock_cloud = mock_property()
        mock_cloud.evaluate_relation.return_value = None
        event = mock.MagicMock()
        harness.charm.cloud_integration.integrate(TEST_CLUSTER_NAME, event)

        if harness.charm.is_worker:
            mock_cloud.tag_instance.assert_called_once_with({"k8s-io-cluster-name": "my-cluster"})
        else:
            mock_cloud.tag_instance.assert_called_once_with(
                {
                    "k8s-io-cluster-name": "my-cluster",
                    "k8s-io-role-master": "master",  # wokeignore:rule=master
                }
            )
        mock_cloud.enable_object_storage_management.assert_called_once()
        if harness.charm.is_worker:
            mock_cloud.enable_security_management.assert_not_called()
            mock_cloud.enable_network_management.assert_not_called()
            mock_cloud.enable_block_storage_management.assert_not_called()
        else:
            mock_cloud.enable_security_management.assert_called_once()
            mock_cloud.enable_network_management.assert_called_once()
            mock_cloud.enable_block_storage_management.assert_called_once()
        mock_cloud.enable_instance_inspection.assert_called_once()
        mock_cloud.enable_dns_management.assert_called_once()
        mock_cloud.evaluate_relation.assert_called_once_with(event)


def test_cloud_azure(harness):
    """Test that the cloud property returns the correct integration requires object.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    harness.charm.get_cloud_name.return_value = "azure"
    with mock.patch(
        "cloud_integration.CloudIntegration.cloud",
        new_callable=mock.PropertyMock,
        return_value=mock.create_autospec(AzureIntegrationRequires),
    ) as mock_property:
        mock_cloud = mock_property()
        mock_cloud.evaluate_relation.return_value = None
        event = mock.MagicMock()
        harness.charm.cloud_integration.integrate(TEST_CLUSTER_NAME, event)
        if harness.charm.is_worker:
            mock_cloud.tag_instance.assert_called_once_with({"k8s-io-cluster-name": "my-cluster"})
        else:
            mock_cloud.tag_instance.assert_called_once_with(
                {
                    "k8s-io-cluster-name": "my-cluster",
                    "k8s-io-role-master": "master",  # wokeignore:rule=master
                }
            )
        mock_cloud.enable_object_storage_management.assert_called_once()
        if harness.charm.is_worker:
            mock_cloud.enable_security_management.assert_not_called()
            mock_cloud.enable_loadbalancer_management.assert_not_called()
            mock_cloud.enable_network_management.assert_not_called()
            mock_cloud.enable_block_storage_management.assert_not_called()
        else:
            mock_cloud.enable_security_management.assert_called_once()
            mock_cloud.enable_loadbalancer_management.assert_called_once()
            mock_cloud.enable_network_management.assert_called_once()
            mock_cloud.enable_block_storage_management.assert_called_once()
        mock_cloud.enable_dns_management.assert_called_once()
        mock_cloud.enable_instance_inspection.assert_called_once()
        mock_cloud.evaluate_relation.assert_called_once_with(event)


def test_cloud_unknown(harness):
    """Test that the cloud property returns the correct integration requires object.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    harness.charm.get_cloud_name.return_value = "unknown"
    with mock.patch(
        "cloud_integration.CloudIntegration.cloud",
        new_callable=mock.PropertyMock,
        return_value=None,
    ) as mock_property:
        event = mock.MagicMock()
        harness.charm.cloud_integration.integrate(TEST_CLUSTER_NAME, event)
        assert mock_property.called
