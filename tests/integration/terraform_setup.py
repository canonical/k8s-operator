#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""
This script deploys a Kubernetes cluster using the Juju Terraform provider and a
manifest as input. See https://github.com/asbalderson/k8s-bundles/blob/terraform-bundle-basic/terraform/README.md

Key Features:
- Verifies if Terraform is installed and at the specified version. If not,
  installs the correct version using the Snap package manager.
- Sets up Juju provider authentication by exporting necessary credentials
  and connection details from the Juju configuration.
- Checks if a specified Juju model exists; if not, creates the model.
- Applies a specified LXD profile (`k8s.profile`) to the Juju model if the
  controller's cloud is LXD or localhost.
- Initializes Terraform, validates the plan, and applies it with the
  provided manifest and model name.

Inputs (Command-Line Arguments):
- `--version`: Specifies the expected version of Terraform to be installed
  (default: latest/stable).
- `--path`: The path to the Terraform module directory (default: the script's directory).
- `--manifest`: Path to the YAML manifest file used by Terraform (default: `default-manifest.yaml` in the script's directory).
- `--model-name`: The name of the Juju model to operate on (default: `my-canonical-k8s`).

Usage:
    python3 script.py --version latest/stable --path /path/to/module --manifest /path/to/manifest.yaml --model-name custom-model
"""

import argparse
from pathlib import Path
import os
import subprocess
import sys


def run_command(command, capture_output=False):
    """Run a shell command."""
    try:
        result = subprocess.run(
            command, shell=True, check=True, text=True, capture_output=capture_output
        )
        return result.stdout.strip() if capture_output else None
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {command}\n{e}")
        sys.exit(1)


def ensure_terraform(expected_version):
    """Ensure Terraform is installed and matches the expected version."""
    installed_version = run_command(
        "snap list terraform | awk '/^terraform/ {print $4}'", capture_output=True
    )
    if not installed_version:
        print(f"Terraform is not installed. Installing version {expected_version}...")
        run_command(f"sudo snap install terraform --channel={expected_version} --classic")
    elif installed_version != expected_version:
        print(
            f"Error: Installed Terraform version ({installed_version}) does not match the expected version ({expected_version})."
        )
        sys.exit(1)
    else:
        print(
            f"Terraform is already installed and matches the expected version: {installed_version}."
        )


def setup_juju_provider_authentication():
    """Set up Juju provider authentication.

    See https://registry.terraform.io/providers/juju/juju/latest/docs#authentication
    """
    controller = run_command("juju whoami | yq .Controller", capture_output=True) or ""
    os.environ["CONTROLLER"] = controller or ""
    os.environ["JUJU_CONTROLLER_ADDRESSES"] = (
        run_command(
            f"juju show-controller | yq {controller}.details.api-endpoints | yq -r '. | join(\",\")'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_USERNAME"] = (
        run_command(
            f"cat ~/.local/share/juju/accounts.yaml | yq .controllers.{controller}.user | tr -d '\"'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_PASSWORD"] = (
        run_command(
            f"cat ~/.local/share/juju/accounts.yaml | yq .controllers.{controller}.password | tr -d '\"'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_CA_CERT"] = (
        run_command(
            f"juju show-controller {controller.strip()} | yq '.{controller}.details.\"ca-cert\"' | tr -d '\"' | sed 's/\\\\n/\\n/g'",
            capture_output=True,
        )
        or ""
    )


def ensure_model_exists(model_name, terraform_dir):
    """Ensure the specified Juju model exists."""
    if run_command(f"juju models | grep -q {model_name}", capture_output=True):
        print(f"Juju model '{model_name}' already exists.")
    else:
        print(f"Juju model '{model_name}' does not exist. Creating it...")
        run_command(f"juju add-model {model_name} localhost")

    controller_cloud = run_command(
        "juju show-controller | yq -r .$CONTROLLER.details.cloud", capture_output=True
    )
    if controller_cloud in ["localhost", "lxd"]:
        print(
            f"Current Juju controller is using LXD/localhost. Applying 'k8s.profile' to the model..."
        )
        run_command(f"lxc profile edit juju-{model_name} < {terraform_dir}/k8s.profile")
    else:
        print("Current Juju controller is not LXD/localhost. Skipping 'k8s.profile' application.")


def main():
    script_dir = Path(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser(description="Terraform and Juju setup script.")
    parser.add_argument("--version", default="latest/stable", help="Expected Terraform version.")
    parser.add_argument(
        "--terraform-module-path",
        default=script_dir / "data",
        help="Path to the Terraform module that should be deployed.",
    )
    parser.add_argument(
        "--lxd-profile-path",
        default=script_dir / "data" / "k8s.profile",
        help="Path to the Terraform module that should be deployed.",
    )
    parser.add_argument(
        "--manifest-path",
        default=script_dir / "default-manifest.yaml",
        help="Path to the manifest YAML file.",
    )
    parser.add_argument("--model-name", default="my-canonical-k8s", help="Name of the Juju model.")

    args = parser.parse_args()

    # Install or validate Terraform
    ensure_terraform(args.version)

    # Set up Juju provider authentication
    setup_juju_provider_authentication()

    # Ensure the Juju model exists
    ensure_model_exists(args.model_name, args.path)

    # Change to the Terraform module directory
    os.chdir(args.path)

    # Run Terraform commands
    print("Running 'terraform init'")
    run_command("terraform init")

    print(
        f"Running 'terraform plan' with manifest: {args.manifest} and model: {args.model_name}..."
    )
    run_command(
        f"terraform plan -var='manifest_path={args.manifest}' -var='model_name={args.model_name}'"
    )

    print(
        f"Running 'terraform apply' with manifest: {args.manifest} and model: {args.model_name}..."
    )
    run_command(
        f"terraform apply -var='manifest_path={args.manifest}' -var='model_name={args.model_name}' -auto-approve"
    )


if __name__ == "__main__":
    main()
