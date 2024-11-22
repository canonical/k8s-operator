# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests."""


from pathlib import Path

import ops
import ops.testing
import pytest
from charm import K8sCharm


@pytest.fixture(params=["worker", "control-plane"])
def harness(request):
    """Craft a ops test harness.

    Args:
        request: pytest request object
    """
    meta = Path(__file__).parent / "../../charmcraft.yaml"
    if request.param == "worker":
        meta = Path(__file__).parent / "../../../charmcraft.yaml"
    harness = ops.testing.Harness(K8sCharm, meta=meta.read_text())
    harness.begin()
    harness.charm.is_worker = request.param == "worker"
    yield harness
    harness.cleanup()


def test_configure_network_options(harness):
    """Test configuring the network options.

    Args:
        harness: the harness under test
    """
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")

    harness.disable_hooks()

    harness.update_config({"network-enabled": False})
    ufcg = harness.charm._assemble_cluster_config()
    assert not ufcg.network.enabled, "Network should be disabled"

    harness.update_config({"network-enabled": True})
    ufcg = harness.charm._assemble_cluster_config()
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
    default_tls_secret = "my-secret"

    harness.update_config({"ingress-enabled": enabled})
    harness.update_config({"ingress-enable-proxy-protocol": proxy_protocol_enabled})
    harness.update_config({"ingress-default-tls-secret": default_tls_secret})

    ufcg = harness.charm._assemble_cluster_config()
    assert ufcg.ingress.enabled == enabled
    assert ufcg.ingress.enable_proxy_protocol == proxy_protocol_enabled
    assert ufcg.ingress.default_tls_secret == default_tls_secret
