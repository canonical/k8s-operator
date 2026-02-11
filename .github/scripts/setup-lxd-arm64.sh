#!/bin/bash
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

set -eux

ARCH=$(uname -m)
if [ "$ARCH" != "aarch64" ]; then
  echo "Not running on ARM64 (arch=${ARCH}), skipping LXD reconfiguration."
  exit 0
fi

CONTROLLER=$(lxc list --format csv --columns n | grep '^juju-' | head -n1)

if [ -z "$CONTROLLER" ]; then
  echo "ERROR: No Juju controller container found to fix."
  lxc list
  exit 1
fi
echo "Found controller: ${CONTROLLER}"

lxc config set "${CONTROLLER}" security.nesting true
lxc config set "${CONTROLLER}" security.privileged true
lxc config set "${CONTROLLER}" raw.lxc "lxc.apparmor.profile=unconfined"
echo "LXD security profile updated."

lxc exec "${CONTROLLER}" -- bash -c "
    set -x
    rm -f juju-db_*.snap
    snap download juju-db --channel=4.4/stable
    snap install ./juju-db_*.snap --dangerous --devmode
"

lxc restart "${CONTROLLER}"

MAX_RETRIES=30
for ((i = 1; i <= MAX_RETRIES; i++)); do
  if juju status -m controller >/dev/null 2>&1; then
    echo "Juju API is reachable."
    break
  fi
  echo "Waiting for Juju API (attempt $i/$MAX_RETRIES)..."
  sleep 10
done

MAX_JSON_RETRIES=20
for ((i = 1; i <= MAX_JSON_RETRIES; i++)); do
  # Capture the status json
  STATUS_JSON=$(juju status -m controller --format json)

  APP_STATUS=$(echo "$STATUS_JSON" | jq -r '.applications.controller["application-status"].current')
  UNIT_STATUS=$(echo "$STATUS_JSON" | jq -r '.applications.controller.units | to_entries[0].value["juju-status"].current')

  echo "Check $i: App Status='$APP_STATUS', Unit Status='$UNIT_STATUS'"

  if [ "$APP_STATUS" == "active" ] && [ "$UNIT_STATUS" == "idle" ]; then
    echo "SUCCESS: Controller is fully operational (Active/Idle)."
    exit 0
  fi

  echo "Controller not ready yet. Waiting..."
  sleep 5
done

echo "TIMEOUT: Controller did not reach Active/Idle state."
exit 1
