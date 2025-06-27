# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing
"""configure unit tests."""

from pathlib import Path

import ops.testing
import pytest

import charm


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    meta = Path(charm.__file__).parent / "../charmcraft.yaml"
    if request.param == "worker":
        meta = Path(charm.__file__).parent / "../../charmcraft.yaml"
    harness = ops.testing.Harness(charm.K8sCharm, meta=meta.read_text())
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


@pytest.fixture(autouse=True)
def mock_juju_public_address(monkeypatch):
    """Mock _get_juju_public_address to return."""
    monkeypatch.setattr(charm, "_get_juju_public_address", lambda: "127.0.0.1")
