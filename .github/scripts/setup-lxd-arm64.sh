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

END_TIME=$(($(date +%s) + 600))

while true; do
  CURRENT_TIME=$(date +%s)
  if [ $CURRENT_TIME -gt $END_TIME ]; then
    echo "TIMEOUT: Controller did not stabilize within 10 minutes."
    exit 1
  fi

  echo "Checking Juju status..."

  set +e # Temporarily allow failure so we can handle the exit code
  STATUS_JSON=$(timeout 60s juju status -m controller --format json 2>/dev/null)
  EXIT_CODE=$?
  set -e # Re-enable strict mode

  # Check exit codes
  if [ $EXIT_CODE -eq 124 ]; then
    echo " > Command timed out (hung > 60s). Retrying..."
    sleep 5
    continue
  elif [ $EXIT_CODE -ne 0 ]; then
    echo " > Controller unreachable (Exit code $EXIT_CODE). Retrying..."
    sleep 10
    continue
  fi

  APP_STATUS=$(echo "$STATUS_JSON" | jq -r '.applications.controller["application-status"].current')
  UNIT_STATUS=$(echo "$STATUS_JSON" | jq -r '.applications.controller.units | to_entries[0].value["juju-status"].current')

  echo " > Current State: App=$APP_STATUS | Agent=$UNIT_STATUS"

  if [ "$APP_STATUS" == "active" ] && [ "$UNIT_STATUS" == "idle" ]; then
    echo "SUCCESS: Controller is fully operational."
    break
  fi

  echo " > Waiting for Active/Idle state..."
  sleep 5
done
