#!/bin/bash
# This script ensures that the Terraform snap is installed and at the expected version.
# If Terraform is not installed, it installs it. If the installed version does not match
# the expected version, the script exits with an error message.
# After verifying Terraform, the script prints the output of `terraform plan` and then
# runs `terraform apply` without confirmation. It also sets up Juju provider authentication,
# ensures the specified model exists, and passes the model name and manifest path to Terraform commands.
#
# Inputs:
#   - `--version`: Expected Terraform version (default: latest/stable)
#   - `--path`: Path to the Terraform module (default: ./)
#   - `--manifest`: Path to the manifest YAML file (default: ./default_manifest.yaml)
#   - `--model-name`: Juju model name (default: my-canonical-k8s)
#
# Usage:
#   ./script.sh --version latest/stable --path /path/to/module --manifest /path/to/manifest.yaml --model-name custom-model

set -ex

# Default values
EXPECTED_VERSION="latest/stable"
MODULE_PATH="./"
MANIFEST_PATH="./default_manifest.yaml"
MODEL_NAME="my-canonical-k8s"
TERRAFORM_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Parse inputs
while [[ $# -gt 0 ]]; do
  case $1 in
    --version)
      EXPECTED_VERSION="$2"
      shift 2
      ;;
    --path)
      MODULE_PATH="$2"
      shift 2
      ;;
    --manifest)
      MANIFEST_PATH="$2"
      shift 2
      ;;
    --model-name)
      MODEL_NAME="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

# Function to get the current Terraform version installed via snap
get_installed_version() {
  snap list terraform 2>/dev/null | awk '/^terraform/ {print $4}'
}

# Check if Terraform is installed and matches the expected version
INSTALLED_VERSION=$(get_installed_version)
if [[ -z "$INSTALLED_VERSION" ]]; then
  echo "Terraform is not installed. Installing version $EXPECTED_VERSION..."
  sudo snap install terraform --channel="$EXPECTED_VERSION"
elif [[ "$INSTALLED_VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "Error: Installed Terraform version ($INSTALLED_VERSION) does not match the expected version ($EXPECTED_VERSION)."
  exit 1
else
  echo "Terraform is already installed and matches the expected version: $INSTALLED_VERSION."
fi

# Set up Juju provider authentication
setup_juju_provider_authentication() {
  export CONTROLLER=$(juju whoami | yq .Controller)
  export JUJU_CONTROLLER_ADDRESSES=$(juju show-controller | yq .$CONTROLLER.details.api-endpoints | yq -r '. | join(",")')
  export JUJU_USERNAME="$(cat ~/.local/share/juju/accounts.yaml | yq .controllers.$CONTROLLER.user | tr -d '"')"
  export JUJU_PASSWORD="$(cat ~/.local/share/juju/accounts.yaml | yq .controllers.$CONTROLLER.password | tr -d '"')"
  export JUJU_CA_CERT="$(juju show-controller $(echo $CONTROLLER | tr -d '"') | yq '.[$CONTROLLER]'.details.\"ca-cert\" | tr -d '"' | sed 's/\\n/\n/g')"
}

# Ensure the specified Juju model exists
ensure_model_exists() {
  if juju models | grep -q "$MODEL_NAME"; then
    echo "Juju model '$MODEL_NAME' already exists."
  else
    echo "Juju model '$MODEL_NAME' does not exist. Creating it..."
    # TODO(ben): Make the cloud configurable?
    juju add-model "$MODEL_NAME" localhost
  fi

  # Check if the current Juju controller is using LXD/localhost
  CONTROLLER_CLOUD=$(juju show-controller | yq -r ".$CONTROLLER.details.cloud")
  if [[ "$CONTROLLER_CLOUD" == "localhost" || "$CONTROLLER_CLOUD" == "lxd" ]]; then
    echo "Current Juju controller is using LXD/localhost. Applying 'k8s.profile' to the model..."
    lxc profile edit juju-"$MODEL_NAME" < "$TERRAFORM_DIR"/k8s.profile
  else
    echo "Current Juju controller is not LXD/localhost. Skipping 'k8s.profile' application."
  fi
}

setup_juju_provider_authentication
ensure_model_exists

echo "Running 'terraform init'"
terraform init

# Pass the manifest path and model name as Terraform variables and print the Terraform plan
echo "Running 'terraform plan' with manifest: $MANIFEST_PATH and model: $MODEL_NAME..."
terraform plan -var="manifest_path=$MANIFEST_PATH" -var="model_name=$MODEL_NAME"

# Apply the Terraform configuration without confirmation
echo "Running 'terraform apply' with manifest: $MANIFEST_PATH and model: $MODEL_NAME..."
terraform apply -var="manifest_path=$MANIFEST_PATH" -var="model_name=$MODEL_NAME" -auto-approve
