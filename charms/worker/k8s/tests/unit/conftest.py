# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pytest configuration file for the charm tests."""

import charm
import pytest


@pytest.fixture(autouse=True)
def mock_juju_public_address(monkeypatch):
    """Mock _get_juju_public_address to return."""
    monkeypatch.setattr(charm, "_get_juju_public_address", lambda: "127.0.0.1")
