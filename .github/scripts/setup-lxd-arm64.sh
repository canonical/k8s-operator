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

# Find the Juju controller container
# We grab the first container starting with 'juju-'
CONTROLLER=$(lxc list --format csv --columns n | grep '^juju-' | head -n1)

if [ -z "$CONTROLLER" ]; then
  echo "ERROR: No Juju controller container found."
  lxc list
  exit 1
fi

echo "Found controller: ${CONTROLLER}"

# Apply security settings DIRECTLY to the container instance.
# This ensures they take precedence over any profile inheritance.
echo "Applying unconfined settings to ${CONTROLLER}..."

# 1. Allow nesting (Crucial for snapd/mongodb to work)
lxc config set "${CONTROLLER}" security.nesting true

# 2. Enable privileged mode (Fixes the /sys/kernel permission errors)
lxc config set "${CONTROLLER}" security.privileged true

# 3. Disable AppArmor (Removes confinement restrictions)
lxc config set "${CONTROLLER}" raw.lxc "lxc.apparmor.profile=unconfined"

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
  sleep 5
done

# Verification step
# We fetch the PID of the container's init process and check the owner on the host.
echo "Verifying confinement status..."
sleep 2 # Give a moment for the PID to be stable
CONTAINER_PID=$(lxc info "${CONTROLLER}" | grep 'PID:' | awk '{print $2}')

# Check the user owner of that PID
REAL_USER=$(ps -o user= -p "${CONTAINER_PID}")

echo "---------------------------------------------------"
echo "Verification Results:"
echo "Container PID: ${CONTAINER_PID}"
echo "Running as User: ${REAL_USER}"

if [ "$REAL_USER" == "root" ]; then
  echo "SUCCESS: The controller is running as privileged root."
else
  echo "WARNING: The controller is running as user '${REAL_USER}' (mapped)."
  echo "It is NOT fully privileged yet."
  exit 1
fi
echo "---------------------------------------------------"
