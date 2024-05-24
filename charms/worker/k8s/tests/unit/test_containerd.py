# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests containerd module."""
import unittest.mock as mock
from os import getgid, getuid

import containerd
import pytest
import tomli_w


def test_ensure_block():
    """Test ensure block method."""
    t = containerd._ensure_block("source\ndata", "inner-block", "# {mark} managed by test")
    assert (
        t
        == """source
data
# begin managed by test
inner-block
# end managed by test
"""
    )

    t = containerd._ensure_block(t, "new-block", "# {mark} managed by test")
    assert (
        t
        == """source
data
# begin managed by test
new-block
# end managed by test
"""
    )


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
        "url": "http://ghcr.io",
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
    assert parsed == [expected]


@pytest.mark.parametrize(
    "registry_errors",
    [
        ("{", "not valid JSON"),
        ("{}", "value is not a valid list"),
        ("[1]", "value is not a valid dict"),
        ("[{}]", "url\n  field required"),
        ('[{"url": 1}]', "invalid or missing URL scheme"),
        (
            '[{"url": "http://ghcr.io", "why-am-i-here": "abc"}]',
            "extra fields not permitted",
        ),
        (
            '[{"url": "http://ghcr.io"}, {"url": "http://ghcr.io"}]',
            "duplicate host definitions: ghcr.io",
        ),
    ],
    ids=[
        "Invalid JSON",
        "Not a List",
        "List Item not an object",
        "Missing required field",
        "Invalid URL",
        "Restricted field",
        "Duplicate host",
    ],
)
def test_registry_parse_failures(registry_errors):
    """Test default registry parsing."""
    registries, expected = registry_errors
    with pytest.raises(ValueError) as e:
        containerd.parse_registries(registries)
    assert expected in str(e.value)


def test_registry_methods():
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

    registry.ca_file_path == containerd.CONFIG_PATH / "ghcr-mirror.io/ca.crt"
    registry.cert_file_path == containerd.CONFIG_PATH / "ghcr-mirror.io/cert.crt"
    registry.key_file_path == containerd.CONFIG_PATH / "ghcr-mirror.io/key.crt"
    registry.hosts_toml_path == containerd.CONFIG_PATH / "ghcr-mirror.io/hosts.toml"

    registry.auth_config == {
        "ghcr-mirror.io": {
            "username": "user",
            "password": "pass",
        }
    }

    registry.password = None
    registry.auth_config == {
        "ghcr-mirror.io": {
            "identitytoken": "token",
        }
    }

    registry.hosts_toml == {
        "server": "http://ghcr.io",
        "host": {
            "capabilities": ["pull", "resolve"],
            "http://ghcr.io": {
                "ca": str(registry.ca_file_path),
                "client": [[str(registry.cert_file_path), str(registry.key_file_path)]],
                "skip_verify": True,
                "override_path": True,
            },
        },
    }
    registry.key_file = None
    registry.hosts_toml == {
        "server": "http://ghcr.io",
        "host": {
            "capabilities": ["pull", "resolve"],
            "http://ghcr.io": {
                "ca": str(registry.ca_file_path),
                "client": str(registry.cert_file_path),
                "skip_verify": True,
                "override_path": True,
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
                    registry.hosts_toml_path, tomli_w.dumps(registry.hosts_toml), 0o600, 0, 0
                ),
            ]
        )


@mock.patch("containerd._ensure_file")
@mock.patch("containerd.subprocess.run")
def test_ensure_registry_configs(mock_run, mock_ensure_file):
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
    assert mock_ensure_file.call_count == 5, "5 files should be written"
    mock_run.assert_called_once_with(["/usr/bin/snap", "restart", "k8s.containerd"])
