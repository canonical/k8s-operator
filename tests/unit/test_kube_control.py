# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests for the kube_control module (see issue #973)."""

import json
import unittest.mock as mock
from pathlib import Path

import kube_control
import pytest


@pytest.fixture
def cp_harness(harness, monkeypatch):
    """Restrict the shared harness fixture to the control-plane charm."""
    if harness.charm.is_worker:
        pytest.skip("kube-control is only configured on the control-plane charm")
    harness.disable_hooks()
    harness.set_leader(True)
    harness.charm.api_manager.get_cluster_status = mock.MagicMock(return_value=None)
    harness.charm.api_manager.revoke_auth_token = mock.MagicMock()
    # Avoid touching the real /etc/kubernetes on the test-running machine.
    monkeypatch.setattr(type(harness.charm), "kubeconfig", Path("/nonexistent-kubeconfig"))
    return harness


def _seed_stale_cred(harness, relation_id, secret_id):
    """Seed the kube-control databag with a credential for a departed unit."""
    creds = {
        "cinder-csi/0": {
            "client_token": "",
            "kubelet_token": "",
            "proxy_token": "",
            "scope": "cinder-csi/0",
            "secret-id": secret_id,
        }
    }
    harness.update_relation_data(
        relation_id, harness.charm.unit.name, {"creds": json.dumps(creds)}
    )


def test_configure_purges_creds_with_missing_secret(cp_harness):
    """Regression test for #973: a stale cred with a deleted secret no longer crashes."""
    relation_id = cp_harness.add_relation("kube-control", "cinder-csi")
    _seed_stale_cred(cp_harness, relation_id, "secret:nonexistent000")

    kube_control.configure(cp_harness.charm)

    creds = json.loads(
        cp_harness.get_relation_data(relation_id, cp_harness.charm.unit.name)["creds"]
    )
    assert "cinder-csi/0" not in creds
    cp_harness.charm.api_manager.revoke_auth_token.assert_not_called()


def test_configure_revokes_other_creds_when_one_secret_is_missing(cp_harness):
    """A single missing secret shouldn't stop revocation of other closed creds."""
    relation_id = cp_harness.add_relation("kube-control", "cinder-csi")
    creds = {
        "cinder-csi/1": {
            "client_token": "still-valid-token",
            "kubelet_token": "",
            "proxy_token": "",
            "scope": "cinder-csi/1",
            "secret-id": None,
        },
        "cinder-csi/0": {
            "client_token": "",
            "kubelet_token": "",
            "proxy_token": "",
            "scope": "cinder-csi/0",
            "secret-id": "secret:nonexistent000",
        },
    }
    cp_harness.update_relation_data(
        relation_id, cp_harness.charm.unit.name, {"creds": json.dumps(creds)}
    )

    kube_control.configure(cp_harness.charm)

    cp_harness.charm.api_manager.revoke_auth_token.assert_called_once_with("still-valid-token")
