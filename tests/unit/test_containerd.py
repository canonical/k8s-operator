# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests containerd module."""

from os import getgid, getuid
from pathlib import Path
from unittest import mock

import containerd
import pytest
import tomli_w


def test_ensure_file(tmp_path):
    """Test ensure file method."""
    test_file = tmp_path / "test.txt"
    assert containerd._ensure_file(test_file, "data", 0o644, getuid(), getgid())
    assert test_file.read_text() == "data"
    assert test_file.stat().st_mode == 0o100644
    assert test_file.stat().st_uid == getuid()
    assert test_file.stat().st_gid == getgid()


def test_registry_parse_default():
    """Test default registry parsing."""
    assert containerd.parse_registries("[]") == []


def test_registry_parse_all_fields():
    """Test default registry parsing all fields."""
    parsed = containerd.parse_registries(
        """[{
        "host": "ghcr.io",
        "url": "https://custom-registry/v2/ghcr.io",
        "ca_file": "Y2FfZmlsZQ==",
        "cert_file": "Y2VydF9maWxl",
        "key_file": "a2V5X2ZpbGU=",
        "username": "user",
        "password": "pass",
        "identitytoken": "token",
        "skip_verify": true,
        "override_path": true
    }]"""
    )
    expected = containerd.Registry(
        host="ghcr.io",
        url="https://custom-registry/v2/ghcr.io",
        ca_file="Y2FfZmlsZQ==",
        cert_file="Y2VydF9maWxl",
        key_file="a2V5X2ZpbGU=",
        username="user",
        password="pass",
        identitytoken="token",
        skip_verify=True,
        override_path=True,
    )
    assert parsed == [expected]


@pytest.mark.parametrize(
    "registry_errors",
    [
        ("{", "not valid YAML"),
        ("{}", "Input should be a valid list"),
        ("[1]", "Input should be a valid dictionary"),
        ("[{}]", "url\n  Field required"),
        ('[{"url": 1}]', "Input should be a valid string"),
        ('[{"url": "not-a-url"}]', "Input should be a valid URL"),
        (
            '[{"url": "http://ghcr.io", "why-am-i-here": "abc"}]',
            "Extra inputs are not permitted",
        ),
        (
            '[{"url": "http://ghcr.io"}, {"url": "http://ghcr.io"}]',
            "duplicate host definitions: ghcr.io",
        ),
        (
            '[{"url": "http://ghcr.io:443"}, {"url": "http://ghcr.io:443"}]',
            "duplicate host definitions: ghcr.io:443",
        ),
    ],
    ids=[
        "Invalid YAML",
        "Not a List",
        "List Item not an object",
        "Missing required field",
        "URL not a string",
        "Invalid URL",
        "Restricted field",
        "Duplicate host",
        "Duplicate host with port",
    ],
)
def test_registry_parse_failures(registry_errors):
    """Test default registry parsing."""
    registries, expected = registry_errors
    with pytest.raises(ValueError) as e:
        containerd.parse_registries(registries)
    assert expected in str(e.value)


@mock.patch("containerd.hostsd_path")
def test_registry_methods(hostsd_path, tmp_path):
    """Test registry methods."""
    hostsd_path.return_value = test_path = tmp_path / "hostsd"

    registry = containerd.Registry(
        host="ghcr-mirror.io",
        url="http://ghcr.io/",
        ca_file="Y2FfZmlsZQ==",
        cert_file="Y2VydF9maWxl",
        key_file="a2V5X2ZpbGU=",
        username="user",
        password="pass",
        identitytoken="token",
        skip_verify=True,
        override_path=True,
    )

    assert registry.ca_file_path == test_path / "ghcr-mirror.io/ca.crt"
    assert registry.cert_file_path == test_path / "ghcr-mirror.io/client.crt"
    assert registry.key_file_path == test_path / "ghcr-mirror.io/client.key"
    assert registry.hosts_toml_path == test_path / "ghcr-mirror.io/hosts.toml"

    assert registry.auth_config_header == {"Authorization": "Basic dXNlcjpwYXNz"}

    registry.password = None
    assert registry.auth_config_header == {"Authorization": "Bearer token"}

    assert registry.hosts_toml == {
        "server": "http://ghcr.io/",
        "host": {
            "http://ghcr.io/": {
                "capabilities": ["pull", "resolve"],
                "ca": str(registry.ca_file_path),
                "client": [[str(registry.cert_file_path), str(registry.key_file_path)]],
                "skip_verify": True,
                "override_path": True,
                "header": {"Authorization": "Bearer token"},
            },
        },
    }
    registry.key_file = None
    assert registry.hosts_toml == {
        "server": "http://ghcr.io/",
        "host": {
            "http://ghcr.io/": {
                "capabilities": ["pull", "resolve"],
                "ca": str(registry.ca_file_path),
                "client": str(registry.cert_file_path),
                "skip_verify": True,
                "override_path": True,
                "header": {"Authorization": "Bearer token"},
            },
        },
    }

    registry.key_file = "key_file"
    with mock.patch("containerd._ensure_file") as ensure_file:
        registry.ensure_certificates()
        ensure_file.assert_has_calls(
            [
                mock.call(registry.ca_file_path, "ca_file", 0o600, 0, 0),
                mock.call(registry.cert_file_path, "cert_file", 0o600, 0, 0),
                mock.call(registry.key_file_path, "key_file", 0o600, 0, 0),
            ]
        )

    with mock.patch("containerd._ensure_file") as ensure_file:
        registry.ensure_hosts_toml()
        ensure_file.assert_has_calls(
            [
                mock.call(
                    registry.hosts_toml_path,
                    tomli_w.dumps(registry.hosts_toml),
                    0o600,
                    0,
                    0,
                ),
            ]
        )


@mock.patch("containerd.hostsd_path", mock.Mock(return_value=Path("/path/to/hostsd")))
@mock.patch("containerd._ensure_file")
def test_ensure_registry_configs(mock_ensure_file):
    """Test registry methods."""
    registry = containerd.Registry(
        host="ghcr-mirror.io",
        url="http://ghcr.io",
        ca_file="Y2FfZmlsZQ==",
        cert_file="Y2VydF9maWxl",
        key_file="a2V5X2ZpbGU=",
        username="user",
        password="pass",
        identitytoken="token",
        skip_verify=True,
        override_path=True,
    )

    containerd.ensure_registry_configs([registry])
    assert mock_ensure_file.call_count == 4, "4 files should be written"


def test_ensure_registry_host_mapped_from_url():
    """Test registry methods."""
    registry = containerd.Registry(url="http://ghcr.io")
    assert registry.host == "ghcr.io", "Host from URL"
    assert registry.auth_config_header == {}

    registry = containerd.Registry(url="http://ghcr.io:443")
    assert registry.host == "ghcr.io:443", "Host from URL with port"
    assert registry.auth_config_header == {}

    registry = containerd.Registry(url="http://ghcr.io/v2/my-path")
    assert registry.host == "ghcr.io", "Host from URL without path"
    assert registry.auth_config_header == {}
