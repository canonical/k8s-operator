# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""

import contextlib
import itertools
import json
from pathlib import Path
from unittest import mock

import containerd
import k8s.node
import ops
import pytest
from charmlibs.snap import SnapError
from charms.contextual_status import ReconcilerError
from k8sd_api_manager import (
    BootstrapConfig,
    UpdateClusterConfigRequest,
)
from literals import BOOTSTRAP_CERTIFICATES, DEFAULT_CERTIFICATE_PROVIDER
from mocks import MockELBRequest, MockELBResponse, MockEvent  # pylint: disable=import-error


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
    harness.charm.upgrade = mock.MagicMock()
    harness.charm._ensure_cert_sans = mock.MagicMock()
    public_addr = "11.12.13.14"
    remote_addr = "11.12.13.15"
    harness.disable_hooks()
    harness.add_relation(
        "cluster",
        harness.charm.app.name,
        app_data={BOOTSTRAP_CERTIFICATES.name: DEFAULT_CERTIFICATE_PROVIDER},
    )
    harness.charm.bootstrap.immutable = harness.charm.bootstrap.load_immutable()
    if harness.charm.is_control_plane:
        harness.add_network(
            public_addr, endpoint="cluster", ingress_addresses=[public_addr, remote_addr]
        )
    harness.enable_hooks()
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.ActiveStatus("Ready")
    assert harness.charm.reconciler.stored.reconciled
    called = {name: h for name, h in handlers.items() if h.called}
    assert len(called) == len(handlers)


def test_configure_datastore_bootstrap_config_auto(harness):
    """Test configuring the datastore=auto on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    bs_config = BootstrapConfig()
    harness.update_config()
    harness.charm._configure_datastore(bs_config)
    assert bs_config.datastore_ca_cert is None
    assert bs_config.datastore_client_cert is None
    assert bs_config.datastore_client_key is None
    assert bs_config.datastore_servers is None
    assert bs_config.datastore_type is None


def test_configure_datastore_bootstrap_config_managed_etcd(harness):
    """Test configuring the datastore=managed-etcd on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    bs_config = BootstrapConfig()
    harness.update_config({"bootstrap-datastore": "managed-etcd"})
    harness.charm._configure_datastore(bs_config)
    assert bs_config.datastore_ca_cert is None
    assert bs_config.datastore_client_cert is None
    assert bs_config.datastore_client_key is None
    assert bs_config.datastore_servers is None
    assert bs_config.datastore_type == "etcd"


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


@mock.patch("containerd.hostsd_path", mock.Mock(return_value=Path("/path/to/hostsd")))
def test_set_leader_etcd_missing(harness):
    """Test emitting the set_leader hook while not reconciled.

    Args:
        mock_detect_bootstrap: mock for detect_bootstrap_config_changes
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.charm.reconciler.stored.reconciled = False  # Pretended to not be reconciled
    harness.charm._ensure_cert_sans = mock.MagicMock()
    harness.update_config({"bootstrap-datastore": "etcd"})
    public_addr = "11.12.13.14"
    remote_addr = "11.12.13.15"
    if harness.charm.is_control_plane:
        harness.add_network(
            public_addr, endpoint="cluster", ingress_addresses=[public_addr, remote_addr]
        )
        harness.add_relation("cluster", "remote")
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.BlockedStatus("Missing etcd relation")
    harness.add_relation("etcd", "etcd")
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.ActiveStatus("Ready")
    # NOTE: (mateo) Skipping this fragment until the integration is added again.
    """
    etcd_client_relation = harness.add_relation("etcd-client", "charmed-etcd")
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.BlockedStatus(
        "etcd and etcd-client relations are mutually exclusive"
    )

    harness.remove_relation(etcd_client_relation)
    etcd_certificates_relation = harness.add_relation("etcd-certificates", "ssc")
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.BlockedStatus(
        "etcd-certificates relation is incompatible with etcd relation"
    )
    harness.remove_relation(etcd_certificates_relation)
    harness.remove_relation(etcd_relation)
    harness.add_relation("etcd-client", "charmed-etcd")
    with mock_reconciler_handlers(harness) as handlers:
        handlers["_evaluate_removal"].return_value = False
        harness.set_leader(True)
    assert harness.model.unit.status == ops.BlockedStatus(
        "etcd-client relation requires etcd-certificates relation"
    )
    """


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


def test_configure_bootstrap_extra_sans(harness):
    """Test configuring kube-apiserver-extra-sans on bootstrap.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.charm._ensure_cert_sans = mock.MagicMock()
    cfg_extra_sans = ["mykubernetes", "mykubernetes.local"]
    public_addr = "11.12.13.14"
    remote_addr = "11.12.13.15"
    harness.add_network(
        public_addr, endpoint="cluster", ingress_addresses=[public_addr, remote_addr]
    )
    harness.add_relation("cluster", "remote")
    harness.update_config({"kube-apiserver-extra-sans": " ".join(cfg_extra_sans)})

    with mock.patch("charm._get_juju_public_address") as m:
        m.return_value = public_addr
        bs_config = harness.charm._assemble_bootstrap_config()

    # We expect the resulting SANs to include the configured addresses as well
    # as the unit address.
    exp_extra_sans = cfg_extra_sans + [public_addr, remote_addr]
    assert len(exp_extra_sans) == len(bs_config.extra_sans)
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


def test_get_public_address_with_external_lb(harness):
    """Test getting the public address with an external load balancer.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    lb_address = "1.2.3.4"

    with mock.patch.object(harness.charm, "external_load_balancer") as mock_elb:
        mock_elb.is_available = True
        mock_elb.get_response.return_value = MockELBResponse(addr=lb_address)
        public_addr = harness.charm._get_public_address()

    assert public_addr == lb_address


def test_get_public_address_without_external_lb(harness):
    """Test getting the public address without an external load balancer.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    exp_public_addr = "1.2.3.4"
    with mock.patch("charm._get_juju_public_address", return_value=exp_public_addr):
        public_addr = harness.charm._get_public_address()
    assert public_addr == exp_public_addr


def test_ensure_cert_sans(harness):
    """Test ensuring certificate SANs are up-to-date.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    with (
        mock.patch.object(harness.charm, "_get_extra_sans") as mock_extra_sans,
        mock.patch("charm.get_certificate_sans", return_value=(["sans1"], ["1.2.3.4"])),
        mock.patch.object(harness.charm.api_manager, "refresh_certs") as mock_api_manager,
    ):
        mock_extra_sans.return_value = ["sans1", "sans2"]
        harness.charm._ensure_cert_sans()
        mock_api_manager.assert_called_once_with(["1.2.3.4", "sans1", "sans2"])


def test_get_external_kubeconfig(harness):
    """Test getting the external kubeconfig.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    public_addr = "1.2.3.4"
    with (
        mock.patch.object(harness.charm, "_get_public_address"),
        mock.patch.object(harness.charm.api_manager, "get_kubeconfig") as mock_api_manager,
    ):
        event = MockEvent(MockEvent.Params())
        mock_api_manager.return_value = {"server": public_addr}
        harness.charm._get_external_kubeconfig(event)
        assert event.results == {"kubeconfig": {"server": public_addr}}

        custom_addr = "10.20.30.40"
        mock_api_manager.return_value = {"server": custom_addr}
        event = MockEvent(MockEvent.Params({"server": custom_addr}))
        harness.charm._get_external_kubeconfig(event)
        assert event.results == {"kubeconfig": {"server": custom_addr}}


def test_configure_external_load_balancer(harness):
    """Test that the external load balancer is configured correctly.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    exp_addr = "1.2.3.4"
    with mock.patch.object(harness.charm, "external_load_balancer") as mock_elb:
        mock_elb.is_available = True
        mock_elb.get_request.return_value = MockELBRequest(MockELBRequest.Protocols())
        mock_elb.get_response = mock.MagicMock()
        mock_elb.get_response.return_value = MockELBResponse(exp_addr)
        harness.charm._configure_external_load_balancer()
        mock_elb.get_response.assert_called_once()


def test_external_load_balancer_address(harness):
    """Test that the external load balancer address is returned correctly.

    Args:
        harness (ops.testing.Harness): The test harness
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    with mock.patch.object(harness.charm, "external_load_balancer") as mock_elb:
        lb_addr = "1.2.3.4"
        mock_elb.is_available = True
        mock_elb.get_response.return_value = MockELBResponse(lb_addr)
        assert harness.charm.external_load_balancer_address == lb_addr


@pytest.fixture
def removing_unit(harness):
    """Prepare a dying unit with simulated node and snap operations."""
    harness.disable_hooks()
    harness.set_leader(True)
    peer = harness.add_relation("upgrade", harness.charm.app.name)
    for number in (1, 2):
        harness.add_relation_unit(peer, f"{harness.charm.app.name}/{number}")
    harness.charm._stored.is_dying = True
    harness.charm._stored.cluster_name = "original-cluster"
    with (
        mock.patch.object(harness.charm, "_evaluate_removal", return_value=True),
        mock.patch.object(
            harness.charm,
            "_apply_proxy_environment",
            side_effect=AssertionError("Removed units must not resume normal reconciliation"),
        ),
        mock.patch(
            "charm.k8s.node.present", return_value=k8s.node.Presence.UNAVAILABLE
        ) as presence,
        mock.patch.object(
            harness.charm.api_manager, "is_cluster_bootstrapped", return_value=False
        ),
        mock.patch.object(harness.charm.update_status, "run"),
        mock.patch.object(harness.charm, "_revoke_cluster_tokens"),
        mock.patch("charm.sleep"),
        mock.patch("charm.snap_management") as remove,
    ):
        harness.enable_hooks()
        yield harness, peer, presence, remove


def test_completed_cleanup_is_not_repeated(removing_unit):
    """Later hooks skip teardown after cleanup removes the snap and its API proxy."""
    harness, peer, presence, remove = removing_unit
    with mock.patch.object(
        harness.charm, "_last_gasp", wraps=harness.charm._last_gasp
    ) as last_gasp:
        harness.charm.on.stop.emit()
        assert harness.charm._stored.cleanup_complete

        presence.side_effect = FileNotFoundError("kubectl was removed with the snap")
        remove.side_effect = SnapError("snap is already removed")
        for number in (1, 2):
            harness.remove_relation_unit(peer, f"{harness.charm.app.name}/{number}")
        harness.charm.on.stop.emit()
        harness.charm.on.remove.emit()
        assert harness.charm._stored.cleanup_complete
        harness.charm.update_status.run.assert_called_once_with()
        last_gasp.assert_called_once_with()
        remove.assert_called_once_with(harness.charm, remove=True)


@pytest.mark.parametrize("leader", [False, True])
@pytest.mark.parametrize("event", ["stop", "remove"])
@pytest.mark.parametrize("presence", [k8s.node.Presence.AVAILABLE, k8s.node.Presence.UNRESPONSIVE])
def test_removal_blocks_while_still_clustered(removing_unit, leader, event, presence):
    """A present Node or still-bootstrapped unit must not uninstall its snap."""
    harness, _, probe, remove = removing_unit
    harness.disable_hooks()
    harness.set_leader(leader)
    harness.enable_hooks()
    probe.return_value = presence
    harness.charm.api_manager.is_cluster_bootstrapped.return_value = True

    getattr(harness.charm.on, event).emit()

    remove.assert_not_called()
    assert not harness.charm._stored.cleanup_complete


def test_cleanup_retries_after_node_deletion_timeout(removing_unit):
    """A later hook can complete cleanup after an uncluster polling timeout."""
    harness, _, presence, remove = removing_unit
    presence.return_value = k8s.node.Presence.AVAILABLE
    harness.charm.on.stop.emit()
    assert not harness.charm._stored.cleanup_complete
    remove.assert_not_called()

    presence.return_value = k8s.node.Presence.UNAVAILABLE
    harness.charm.on.stop.emit()
    assert harness.charm._stored.cleanup_complete
    remove.assert_called_once()


def test_removal_waits_through_api_failure_until_node_disappears(removing_unit):
    """Transient query failures must not prevent eventual cluster removal."""
    harness, _, presence, remove = removing_unit
    states = iter(
        [
            k8s.node.Presence.AVAILABLE,
            k8s.node.Presence.UNRESPONSIVE,
            k8s.node.Presence.UNAVAILABLE,
            k8s.node.Presence.UNAVAILABLE,
            k8s.node.Presence.UNAVAILABLE,
        ]
    )
    current = k8s.node.Presence.AVAILABLE

    def lookup(*args):
        nonlocal current
        current = next(states)
        return current

    def remove_snap(*args, **kwargs):
        assert current == k8s.node.Presence.UNAVAILABLE

    presence.side_effect = lookup
    remove.side_effect = remove_snap
    harness.charm.on.stop.emit()
    assert harness.charm._stored.cleanup_complete


@pytest.mark.parametrize(
    "interruption", [k8s.node.Presence.AVAILABLE, k8s.node.Presence.UNRESPONSIVE]
)
def test_removal_requires_consecutive_completion_observations(removing_unit, interruption):
    """A reappearing Node or a still-clustered API failure resets confirmation."""
    harness, _, presence, remove = removing_unit
    states = itertools.cycle(
        [k8s.node.Presence.UNAVAILABLE, k8s.node.Presence.UNAVAILABLE, interruption]
    )

    def lookup(*args):
        state = next(states)
        harness.charm.api_manager.is_cluster_bootstrapped.return_value = (
            state == k8s.node.Presence.UNRESPONSIVE
        )
        return state

    presence.side_effect = lookup

    harness.charm.on.stop.emit()

    remove.assert_not_called()
    assert not harness.charm._stored.cleanup_complete


@pytest.mark.parametrize("error", [SnapError("snap removal failed"), ReconcilerError("pending")])
def test_cleanup_retries_after_snap_removal_failure(removing_unit, error):
    """Snap failures leave cleanup incomplete and retryable."""
    harness, _, presence, remove = removing_unit
    remove.side_effect = [error, None]
    harness.charm.on.stop.emit()
    assert not harness.charm._stored.cleanup_complete
    assert isinstance(harness.model.unit.status, ops.WaitingStatus)
    presence.return_value = k8s.node.Presence.AVAILABLE
    harness.charm.on.stop.emit()
    assert not harness.charm._stored.cleanup_complete

    presence.return_value = k8s.node.Presence.UNAVAILABLE

    harness.charm.on.stop.emit()
    assert harness.charm._stored.cleanup_complete


@pytest.mark.parametrize(
    "presence", [k8s.node.Presence.UNAVAILABLE, k8s.node.Presence.UNRESPONSIVE]
)
def test_removal_waits_for_uncluster_state(removing_unit, presence):
    """Node deletion or an inconclusive query must not bypass bootstrap state."""
    harness, _, probe, remove = removing_unit
    probe.return_value = presence
    with mock.patch.object(harness.charm.api_manager, "is_cluster_bootstrapped") as bootstrapped:
        bootstrapped.return_value = True
        harness.charm.on.stop.emit()
        remove.assert_not_called()
        assert not harness.charm._stored.cleanup_complete

        bootstrapped.side_effect = [True, False, False, False]
        harness.charm.on.stop.emit()
        assert harness.charm._stored.cleanup_complete
        remove.assert_called_once()
