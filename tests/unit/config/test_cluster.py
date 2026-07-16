# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""

from unittest import mock

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


def test_assemble_annotations_empty(harness):
    """Annotations stay None when cluster-annotations is empty."""
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.update_config({"cluster-annotations": ""})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.annotations is None, "Expected no annotations when config is empty"


def test_assemble_annotations_simple(harness):
    """Simple flat key/value YAML mapping is parsed into annotations."""
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.update_config(
        {"cluster-annotations": "k8sd/v1alpha1/metallb/advertise-all-pools: 'true'"}
    )
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.annotations == {
        "k8sd/v1alpha1/metallb/advertise-all-pools": "true"
    }, f"Unexpected annotations: {ufcg.annotations}"


def test_assemble_annotations_multiline_bgp_peers(harness):
    """Multi-line YAML block literal for bgp-peers is preserved as a string."""
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    bgp_peers_yaml = (
        "- peerAddress: 192.0.2.1\n"
        "  peerASN: 65001\n"
        "  myASN: 65000\n"
        "  nodeSelector:\n"
        "    topology.kubernetes.io/zone: zone-a\n"
    )
    annotation_config = (
        "k8sd/v1alpha1/metallb/bgp-peers: |\n"
        + "".join(f"  {line}\n" for line in bgp_peers_yaml.splitlines())
        + 'k8sd/v1alpha1/metallb/advertise-all-pools: "true"\n'
    )
    harness.update_config({"cluster-annotations": annotation_config})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.annotations is not None, "Expected annotations to be set"
    assert "k8sd/v1alpha1/metallb/bgp-peers" in ufcg.annotations
    assert "k8sd/v1alpha1/metallb/advertise-all-pools" in ufcg.annotations
    assert ufcg.annotations["k8sd/v1alpha1/metallb/advertise-all-pools"] == "true"
    # The bgp-peers value should contain the peer list as a YAML string
    peers_val = ufcg.annotations["k8sd/v1alpha1/metallb/bgp-peers"]
    assert "peerAddress: 192.0.2.1" in peers_val
    assert "peerASN: 65001" in peers_val


def test_assemble_annotations_not_overwritten_when_empty(harness):
    """When cluster-annotations is empty, existing annotations from current config are kept."""
    from k8sd_api_manager import UserFacingClusterConfig

    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()
    harness.update_config({"cluster-annotations": ""})

    existing = UserFacingClusterConfig()
    existing.annotations = {"some-key": "some-value"}

    ufcg = assemble_cluster_config(harness.charm, None, current=existing)
    assert ufcg.annotations == {"some-key": "some-value"}, (
        "Existing annotations should be preserved when cluster-annotations config is empty"
    )
