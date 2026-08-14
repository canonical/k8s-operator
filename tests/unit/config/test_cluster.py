# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""

from unittest import mock

import charms.contextual_status
import ops
import pytest
from config.cluster import assemble_cluster_config


def test_configure_network_options(harness):
    """Test configuring the network options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"network-enabled": False})
    harness.update_config({"kube-proxy-enabled": "true"})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert not ufcg.network.enabled, "Network should be disabled"
    assert ufcg.network.kube_proxy_enabled, "kube-proxy-enabled sholud be True"

    harness.update_config({"network-enabled": True})
    harness.update_config({"kube-proxy-enabled": "false"})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.network.enabled, "Network should be enabled"
    assert not ufcg.network.kube_proxy_enabled, "kube-proxy-enabled sholud be False"

    harness.update_config({"network-enabled": True})
    harness.update_config({"kube-proxy-enabled": "auto"})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.network.enabled, "Network should be enabled"
    assert ufcg.network.kube_proxy_enabled is None, "kube-proxy-enabled should not be set"


def test_configure_ingress_options(harness):
    """Test configuring the ingress options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    enabled = True
    proxy_protocol_enabled = True

    harness.update_config({"ingress-enabled": enabled})
    harness.update_config({"ingress-enable-proxy-protocol": proxy_protocol_enabled})

    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.ingress.enabled == enabled
    assert ufcg.ingress.enable_proxy_protocol == proxy_protocol_enabled


def test_configure_common_extra_args(harness):
    """Test configuring the extra options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.add_relation("cluster", "remote", unit_data={"ingress-address": "1.2.3.4"})
    harness.add_network(
        "10.0.0.10", endpoint="cluster", ingress_addresses=("10.0.0.10", "2001:db8:10::a00:a")
    )
    harness.update_config({"kubelet-extra-args": "v=3 foo=bar flag"})
    harness.update_config({"kube-proxy-extra-args": "v=4 foo=baz flog"})

    with mock.patch("charm._get_juju_public_address") as m:
        m.return_value = "1.1.1.1"
        bootstrap_config = harness.charm._assemble_bootstrap_config()
    assert bootstrap_config.extra_node_kubelet_args == {
        "--v": "3",
        "--foo": "bar",
        # NOTE: (mateoflorido): IPv6 addrs are exploded.
        "--node-ip": "10.0.0.10,2001:0db8:0010:0000:0000:0000:0a00:000a",
        "--flag": "true",
    }
    assert bootstrap_config.extra_node_kube_proxy_args == {
        "--v": "4",
        "--foo": "baz",
        "--flog": "true",
    }


def test_configure_controller_extra_args(harness):
    """Test configuring the extra options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.add_relation("cluster", "remote", unit_data={"ingress-address": "1.2.3.4"})
    harness.update_config({"kube-apiserver-extra-args": "v=3 foo=bar flag"})
    harness.update_config({"kube-controller-manager-extra-args": "v=4 foo=baz flog"})
    harness.update_config({"kube-scheduler-extra-args": "v=5 foo=bat blog"})

    with mock.patch("charm._get_juju_public_address") as m:
        m.return_value = "1.1.1.1"
        bootstrap_config = harness.charm._assemble_bootstrap_config()
    assert bootstrap_config.extra_node_kube_apiserver_args == {
        "--v": "3",
        "--foo": "bar",
        "--flag": "true",
    }
    assert bootstrap_config.extra_node_kube_controller_manager_args == {
        "--v": "4",
        "--foo": "baz",
        "--flog": "true",
    }
    assert bootstrap_config.extra_node_kube_scheduler_args == {
        "--v": "5",
        "--foo": "bat",
        "--blog": "true",
    }


def test_configure_datastore_extra_args(harness):
    """Test configuring the datastore extra options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.add_relation("cluster", "remote", unit_data={"ingress-address": "1.2.3.4"})
    harness.update_config(
        {"bootstrap-datastore": "managed-etcd", "datastore-extra-args": "v=6 foo=ban clog"}
    )

    bootstrap_config = harness.charm._assemble_bootstrap_config()

    assert bootstrap_config.extra_node_etcd_args == {
        "--v": "6",
        "--foo": "ban",
        "--listen-metrics-urls": "http://localhost:2381",
        "--clog": "true",
    }


def test_configure_annotations(harness):
    """Test configuring annotations via cluster-annotations charm config.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": "key1=value1 key2=value2"})
    ufcg = assemble_cluster_config(
        harness.charm, None, annotations=harness.charm._get_valid_annotations()
    )
    assert ufcg.annotations == {"key1": "value1", "key2": "value2"}


def test_configure_annotations_not_overwritten_when_empty(harness):
    """Test that existing annotations are preserved when cluster-annotations is unset.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": ""})
    current = assemble_cluster_config(
        harness.charm, None, annotations=harness.charm._get_valid_annotations()
    )
    current.annotations = {"out-of-band": "value"}

    ufcg = assemble_cluster_config(
        harness.charm, None, current, annotations=harness.charm._get_valid_annotations()
    )
    assert ufcg.annotations == {"out-of-band": "value"}


def test_configure_annotations_merged_with_out_of_band(harness):
    """Test that charm-managed annotations merge with out-of-band annotations.

    k8sd merges new annotations with the existing ones, so the charm must
    assemble the merged view: comparing only the configured annotations against
    the full stored map would make every reconciliation appear changed.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": "key1=value1"})
    current = assemble_cluster_config(harness.charm, None)
    current.annotations = {"out-of-band": "value"}

    ufcg = assemble_cluster_config(
        harness.charm, None, current, annotations=harness.charm._get_valid_annotations()
    )
    assert ufcg.annotations == {"out-of-band": "value", "key1": "value1"}


def test_configure_annotations_idempotent(harness):
    """Test repeated reconciliations with out-of-band and charm-managed annotations.

    Once k8sd has stored the merged annotations, further reconciliations must
    not detect a config change.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": "key1=value1"})
    annotations = harness.charm._get_valid_annotations()

    current = assemble_cluster_config(harness.charm, None, annotations=annotations)
    # Simulate an annotation set out-of-band (e.g. via the k8s CLI), merged by k8sd.
    current.annotations["out-of-band"] = "value"

    reconciled = assemble_cluster_config(harness.charm, None, current, annotations=annotations)
    assert reconciled.annotations == {"key1": "value1", "out-of-band": "value"}

    # k8sd stores the merged annotations; subsequent reconciles detect no change.
    assert (
        assemble_cluster_config(harness.charm, None, reconciled, annotations=annotations)
        == reconciled
    )


def test_configure_annotations_removal(harness):
    """Test that a "-" value removes an annotation and converges.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": "key1=-"})
    annotations = harness.charm._get_valid_annotations()

    current = assemble_cluster_config(harness.charm, None)
    current.annotations = {"key1": "value1", "out-of-band": "value"}

    reconciled = assemble_cluster_config(harness.charm, None, current, annotations=annotations)
    # The deletion marker is forwarded while the key is still stored so that
    # k8sd removes it.
    assert reconciled.annotations == {"out-of-band": "value", "key1": "-"}
    assert reconciled != current

    # k8sd applies the deletion; subsequent reconciles detect no change.
    current.annotations = {"out-of-band": "value"}
    assert (
        assemble_cluster_config(harness.charm, None, current, annotations=annotations) == current
    )


@pytest.mark.parametrize("invalid", ["malformed", "key1=", "=value1"])
def test_configure_annotations_malformed(harness, invalid):
    """Test that malformed cluster-annotations raise and block the charm.

    Args:
        harness: the harness under test
        invalid: the invalid cluster-annotations value
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"cluster-annotations": invalid})
    with charms.contextual_status.context(harness.model.unit):
        with pytest.raises(charms.contextual_status.ReconcilerError):
            harness.charm._get_valid_annotations()
    assert harness.model.unit.status == ops.BlockedStatus("Invalid Annotations")
