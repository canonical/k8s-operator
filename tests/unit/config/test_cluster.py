# Copyright 2025 Canonical Ltd.
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
    ufcg = assemble_cluster_config(harness.charm, None)
    assert not ufcg.network.enabled, "Network should be disabled"

    harness.update_config({"network-enabled": True})
    ufcg = assemble_cluster_config(harness.charm, None)
    assert ufcg.network.enabled, "Network should be enabled"


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
    harness.update_config({"kubelet-extra-args": "v=3 foo=bar flag"})
    harness.update_config({"kube-proxy-extra-args": "v=4 foo=baz flog"})

    with mock.patch("charm._get_juju_public_address") as m:
        m.return_value = "1.1.1.1"
        bootstrap_config = harness.charm._assemble_bootstrap_config()
    assert bootstrap_config.extra_node_kubelet_args == {
        "--v": "3",
        "--foo": "bar",
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
    harness.update_config({"bootstrap-datastore": "managed-etcd"})
    harness.update_config({"datastore-extra-args": "v=6 foo=ban clog"})

    bootstrap_config = harness.charm._assemble_bootstrap_config()

    assert bootstrap_config.extra_node_etcd_args == {
        "--v": "6",
        "--foo": "ban",
        "--clog": "true",
    }
    assert bootstrap_config.extra_node_k8s_dqlite_args is None
