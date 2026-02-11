#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Reconfigure the Juju controller LXD container on ARM64 runners.
# This script runs after bootstrap via the pre-run-script hook.

set -eux

ARCH=$(uname -m)
if [ "$ARCH" != "aarch64" ]; then
    echo "Not running on ARM64 (arch=${ARCH}), skipping LXD reconfiguration."
    exit 0
fi

# Apply security settings to the default profile
lxc profile set default raw.lxc "lxc.apparmor.profile=unconfined"
lxc profile set default security.nesting true
lxc profile set default security.privileged true

# Find and restart the Juju controller container
CONTROLLER=$(lxc list --format csv --columns n | grep '^juju-' | head -n1)
if [ -z "$CONTROLLER" ]; then
    echo "ERROR: No Juju controller container found."
    lxc list
    exit 1
fi

echo "Restarting Juju controller container: ${CONTROLLER}"
lxc restart "${CONTROLLER}"

# Wait for the controller to be running again
echo "Waiting for controller to be ready..."
for i in $(seq 1 30); do
    STATUS=$(lxc list "^${CONTROLLER}$" --format csv --columns s)
    if [ "$STATUS" = "RUNNING" ]; then
        echo "Controller ${CONTROLLER} is running."
        break
    fi
    echo "Attempt ${i}/30: status=${STATUS}, waiting..."
    sleep 10
done

# Print debug info
echo "Controller expanded config:"
lxc config show "${CONTROLLER}" --expanded
