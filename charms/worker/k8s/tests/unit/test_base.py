# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""

import contextlib
import json
from pathlib import Path
from unittest import mock

import containerd
import ops
import ops.testing
import pytest
from charm import K8sCharm

from charms.k8s.v0.k8sd_api_manager import BootstrapConfig, UpdateClusterConfigRequest


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
    yield harness
    harness.cleanup()


@contextlib.contextmanager
def mock_reconciler_handlers(harness):
    """Mock out reconciler handlers.

    Args:
        harness: the harness under test

    Yields:
        Mapping of handler_names to their mock methods.
    """
    handler_names = {
        "_evaluate_removal",
        "_install_snaps",
        "_apply_snap_requirements",
        "_check_k8sd_ready",
        "_join_cluster",
        "_configure_cos_integration",
        "_apply_node_labels",
        "_update_kubernetes_version",
    }
    if harness.charm.is_control_plane:
        handler_names |= {
            "_bootstrap_k8s_snap",
            "_create_cluster_tokens",
            "_create_cos_tokens",
            "_apply_cos_requirements",
            "_copy_internal_kubeconfig",
            "_revoke_cluster_tokens",
            "_ensure_cluster_config",
            "_expose_ports",
            "_announce_kubernetes_version",
        }

    mocked = [mock.patch(f"charm.K8sCharm.{name}") for name in handler_names]
    handlers = dict(zip(handler_names, (m.start() for m in mocked)))
    handlers["_update_status"] = mock.patch.object(harness.charm.update_status, "run").start()
    yield handlers
    for handler in handlers.values():
        handler.stop()


def test_config_changed_invalid(harness):
    """Trigger a config-changed event with an unknown-config option.

    Args:
        harness: the harness under test
    """
    with pytest.raises(ValueError):
        harness.update_config({"unknown-config": "foobar"})


def test_update_status(harness):
    """Test emitting the update_status hook while reconciled.

    Args:
        harness: the harness under test
    """
    harness.charm.reconciler.stored.reconciled = True  # Pretended to be reconciled
    harness.model.unit.status = ops.WaitingStatus("Unchanged")
    harness.charm.on.update_status.emit()
    assert harness.model.unit.status == ops.WaitingStatus("Node not Clustered")


@mock.patch("containerd.hostsd_path", mock.Mock(return_value=Path("/path/to/hostsd")))
def test_set_leader(harness):
    """Test emitting the set_leader hook while not reconciled.

    Args:
        harness: the harness under test
    """
    harness.charm.reconciler.stored.reconciled = False  # Pretended to not be reconciled
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.ActiveStatus("Ready")
    assert harness.charm.reconciler.stored.reconciled
    called = {name: h for name, h in handlers.items() if h.called}
    assert len(called) == len(handlers)


def test_configure_datastore_bootstrap_config_dqlite(harness):
    """Test configuring the datastore=dqlite on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    bs_config = BootstrapConfig()
    harness.charm._configure_datastore(bs_config)
    assert bs_config.datastore_ca_cert is None
    assert bs_config.datastore_client_cert is None
    assert bs_config.datastore_client_key is None
    assert bs_config.datastore_servers is None
    assert bs_config.datastore_type is None


def test_configure_datastore_bootstrap_config_etcd(harness):
    """Test configuring the datastore=etcd on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    bs_config = BootstrapConfig()
    harness.update_config({"bootstrap-datastore": "etcd"})
    harness.add_relation("etcd", "etcd")
    with mock.patch.object(harness.charm, "etcd") as mock_etcd:
        mock_etcd.is_ready = True
        mock_etcd.get_client_credentials.return_value = {}
        mock_etcd.get_connection_string.return_value = "foo:1234,bar:1234"
        harness.charm._configure_datastore(bs_config)
    assert bs_config.datastore_ca_cert == ""
    assert bs_config.datastore_client_cert == ""
    assert bs_config.datastore_client_key == ""
    assert bs_config.datastore_servers == ["foo:1234", "bar:1234"]
    assert bs_config.datastore_type == "external"


def test_configure_datastore_runtime_config_dqlite(harness):
    """Test configuring the datastore=dqlite on runtime changes.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    uccr_config = UpdateClusterConfigRequest()
    harness.charm._configure_datastore(uccr_config)
    assert uccr_config.datastore is None


def test_configure_datastore_runtime_config_etcd(harness):
    """Test configuring the datastore=etcd on runtime changes.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.update_config({"bootstrap-datastore": "etcd"})
    harness.add_relation("etcd", "etcd")
    with mock.patch.object(harness.charm, "etcd") as mock_etcd:
        mock_etcd.is_ready = True
        mock_etcd.get_client_credentials.return_value = {}
        mock_etcd.get_connection_string.return_value = "foo:1234,bar:1234"
        uccr_config = UpdateClusterConfigRequest()
        harness.charm._configure_datastore(uccr_config)
    assert uccr_config.datastore
    assert uccr_config.datastore.ca_crt == ""
    assert uccr_config.datastore.client_crt == ""
    assert uccr_config.datastore.client_key == ""
    assert uccr_config.datastore.servers == ["foo:1234", "bar:1234"]
    assert uccr_config.datastore.type == "external"


def test_configure_boostrap_extra_sans(harness):
    """Test configuring kube-apiserver-extra-sans on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    cfg_extra_sans = ["mykubernetes", "mykubernetes.local"]
    public_addr = "11.12.13.14"
    harness.update_config({"kube-apiserver-extra-sans": " ".join(cfg_extra_sans)})

    with mock.patch("charm._get_public_address") as mock_get_public_addr:
        mock_get_public_addr.return_value = public_addr

        bs_config = harness.charm._assemble_bootstrap_config()

    # We expect the resulting SANs to include the configured addresses as well
    # as the unit address.
    exp_extra_sans = cfg_extra_sans + [public_addr]
    for san in exp_extra_sans:
        assert san in bs_config.extra_sans


@mock.patch("containerd.ensure_registry_configs")
def test_config_containerd_registries(mock_ensure_registry_configs, harness):
    """Test configuring containerd registries.

    Args:
        mock_ensure_registry_configs: mock for containerd.ensure_registry_configs
        harness: the harness under test
    """
    harness.disable_hooks()
    cfg_registries = [
        {
            "url": "https://registry.example.com",
            "host": "my.registry:port",
            "username": "user",
            "password": "pass",
        }
    ]
    cfg_data, remote_app = json.dumps(cfg_registries), ""
    app_data = {"custom-registries": cfg_data}
    if harness.charm.is_worker:
        remote_app = "k8s"
        rel = harness.add_relation("containerd", remote_app, app_data=app_data)
    else:
        remote_app = "k8s-worker"
        rel = harness.add_relation("containerd", remote_app)
        harness.set_leader(True)
        harness.update_config({"containerd-custom-registries": cfg_data})
    harness.charm._config_containerd_registries()
    expected = containerd.parse_registries(cfg_data)
    mock_ensure_registry_configs.assert_called_once_with(expected)
    assert app_data == harness.get_relation_data(rel, "k8s")
