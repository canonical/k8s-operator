#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

set -euo pipefail

SONOBUOY_VERSION="${1:-v0.57.3}"
KUBECONFIG_PATH="./kube-config"
SONOBUOY_BIN="/tmp/sonobuoy"
RESULTS_FILE="sonobuoy_e2e.tar.gz"

if ! command -v juju &> /dev/null; then
    echo "Error: 'juju' is not installed."
    exit 1
fi

if ! command -v yq &> /dev/null; then
    echo "Error: 'yq' is not installed."
    exit 1
fi

echo "--> Fetching kubeconfig from k8s/leader..."
juju run k8s/leader get-kubeconfig | yq -r '.kubeconfig' > "$KUBECONFIG_PATH"

if [ ! -s "$KUBECONFIG_PATH" ]; then
    echo "Error: kubeconfig file is empty."
    exit 1
fi

ARCH=$(dpkg --print-architecture)
DOWNLOAD_URL="https://github.com/vmware-tanzu/sonobuoy/releases/download/${SONOBUOY_VERSION}/sonobuoy_${SONOBUOY_VERSION#v}_linux_${ARCH}.tar.gz"

echo "--> Downloading Sonobuoy ($SONOBUOY_VERSION) for $ARCH..."
curl -L "$DOWNLOAD_URL" -o /tmp/sonobuoy.tar.gz

echo "--> Extracting Sonobuoy..."
tar -xzf /tmp/sonobuoy.tar.gz -C /tmp
rm /tmp/sonobuoy.tar.gz

echo "--> Starting Sonobuoy Conformance Run (this may take a while)..."
"$SONOBUOY_BIN" run \
    --kubeconfig "$KUBECONFIG_PATH" \
    --plugin e2e \
    --mode certified-conformance \
    --wait

echo "--> Retrieving results..."
"$SONOBUOY_BIN" retrieve \
    --kubeconfig "$KUBECONFIG_PATH" \
    -f "$RESULTS_FILE"

echo "--> Test Results Summary:"
"$SONOBUOY_BIN" results "$RESULTS_FILE"

echo "--> Done. Full results saved to $RESULTS_FILE"
