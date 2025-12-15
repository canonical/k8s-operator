#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

set -euo pipefail

SONOBUOY_VERSION="${SONOBUOY_VERSION:-v0.57.3}"
DOCKER_REGISTRY_USERNAME="${DOCKER_REGISTRY_USERNAME:-}"
DOCKER_REGISTRY_PASSWORD="${DOCKER_REGISTRY_PASSWORD:-}"
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

if ! command -v juju-wait &> /dev/null; then
     echo "Error: 'juju-wait' is not installed."
     exit 1
fi
if [[ -n "${TF_VAR_model:-}" ]]; then
    export JUJU_MODEL=${TF_VAR_model}
fi
# wait for the workload to be stable
timeout 60m juju-wait -w -v

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


echo "--> Generating sonobuoy manifests for the test"
if [[ -n "$DOCKER_REGISTRY_USERNAME" && -n "$DOCKER_REGISTRY_PASSWORD" ]]; then
    echo "--> Docker registry credentials are provided, configuring them..."
    juju exec --unit k8s/leader -- k8s kubectl create secret docker-registry docker-registry-secret \
        --dry-run=client \
        -o yaml \
        --namespace sonobuoy \
        --docker-server=https://index.docker.io/v1/ \
        --docker-username="$DOCKER_REGISTRY_USERNAME" \
        --docker-password="$DOCKER_REGISTRY_PASSWORD" \
        --docker-email=k8s-team@canonical.com \
        | sed '1s/^/---\n/' >> /tmp/sonobuoy_manifests.yaml
    echo '{"ImagePullSecrets":"docker-registry-secret"}' > /tmp/secretconfig.json
    "$SONOBUOY_BIN" gen \
        --config /tmp/secretconfig.json \
        --kubeconfig "$KUBECONFIG_PATH" > /tmp/sonobuoy_manifests.yaml
else 
    "$SONOBUOY_BIN" gen --kubeconfig "$KUBECONFIG_PATH" > /tmp/sonobuoy_manifests.yaml
fi
    
echo "--> Starting Sonobuoy Conformance Run (this may take a while)..."
"$SONOBUOY_BIN" run \
    --kubeconfig "$KUBECONFIG_PATH" \
    -f /tmp/sonobuoy_manifests.yaml \
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
