# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests for config.arg_files."""

import hashlib
import unittest.mock as mock

from config.arg_files import FileArgsConfig


@mock.patch("pathlib.Path.exists", mock.Mock(return_value=False))
def test_file_args_config():
    """Test the FileArgsConfig class."""
    config = FileArgsConfig()
    assert config.extra_node_kube_apiserver_args == {}
    assert config.extra_node_kube_controller_manager_args == {}
    assert config.extra_node_kube_scheduler_args == {}
    assert config.extra_node_kube_proxy_args == {}
    assert config.extra_node_kubelet_args == {}
    assert config._service_args == {}
    assert config._hash == {}


@mock.patch("pathlib.Path.exists", mock.Mock(return_value=True))
@mock.patch("pathlib.Path.read_text")
def test_file_args_config_with_file(read_text):
    """Test the FileArgsConfig class with a file."""
    read_text.return_value = arg = '--some-arg="value"'
    arg_hash = hashlib.sha256(arg.encode()).digest()
    config = FileArgsConfig()
    expected = {"--some-arg": "value"}

    assert config._service_args == {
        "kube-apiserver": expected,
        "kube-controller-manager": expected,
        "kube-scheduler": expected,
        "kube-proxy": expected,
        "kubelet": expected,
    }
    assert config._hash == {
        "kube-apiserver": arg_hash,
        "kube-controller-manager": arg_hash,
        "kube-scheduler": arg_hash,
        "kube-proxy": arg_hash,
        "kubelet": arg_hash,
    }


@mock.patch("pathlib.Path.exists", mock.Mock(return_value=True))
@mock.patch("pathlib.Path.read_text", mock.Mock(return_value=""))
@mock.patch("pathlib.Path.write_text")
@mock.patch("charms.operator_libs_linux.v2.snap.SnapCache")
def test_file_args_config_ensure_content(snap_cache, write_text):
    """Test the FileArgsConfig class with a file."""
    config = FileArgsConfig()
    config.extra_node_kubelet_args = {"--some-arg": "value"}
    config.ensure()
    snap_cache()["k8s"].restart.assert_called_once_with(["kubelet"])
    write_text.assert_called_once_with('--some-arg="value"\n')
