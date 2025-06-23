# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""proxy service.

This module provides functionality to apply proxy settings to systemd services
based on the Juju model configuration.
"""

import logging

import jmodelproxylib
import ops
from literals import (
    CONTAINERD_HTTP_PROXY,
    CONTAINERD_SERVICE_NAME,
    JUJU_MODEL_PROXY_ENABLE_CONTAINERD,
)

from charms.contextual_status import on_error
from charms.operator_libs_linux.v1 import systemd

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


PROXY_SERVICES = {CONTAINERD_SERVICE_NAME: CONTAINERD_HTTP_PROXY}


@on_error(ops.BlockedStatus("juju-http(s)-proxy is invalid."), ValueError)
def apply(charm: ops.CharmBase) -> None:
    """Apply the current proxy settings to the systemd service files.

    Args:
        charm (ops.CharmBase): The charm instance to apply the proxy settings to.

    """
    proxy_containerd = JUJU_MODEL_PROXY_ENABLE_CONTAINERD.get(charm)

    for service, path in PROXY_SERVICES.items():
        env = jmodelproxylib.environ(enabled=proxy_containerd)
        if env.error:
            raise ValueError(f"Service {service} {env.error}")
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.exists() and path.read_text(encoding="utf-8") or ""
        replacement = jmodelproxylib.systemd(env, service)
        if written := existing != replacement:
            log.info("Applying Proxied Environment Settings for %s", service)
            path.write_text(replacement, encoding="utf-8")
            systemd.daemon_reload()
        if written and systemd.service_running(service):
            # Reload the service to apply the new settings
            log.info("Restarting %s", service)
            systemd.service_restart(service)
        else:
            log.info("No changes to proxy settings for %s, skipping reload", service)
