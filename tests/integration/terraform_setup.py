#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""
This script deploys a Kubernetes cluster using the Juju
Terraform provider and a manifest as input.
See https://github.com/asbalderson/k8s-bundles/blob/terraform-bundle-basic/terraform/README.md

Command-Line Arguments:
- `--version`: Specifies the expected version of Terraform to be installed
  (default: latest/stable).
- `--path`: The path to the Terraform module directory
  (default: the script's directory).
- `--manifest`: Path to the YAML manifest file used by Terraform
  (default: `default-manifest.yaml` in the script's directory).
- `--model-name`: The name of the Juju model to operate on
  (default: `my-canonical-k8s`).

Usage:
    python3 script.py
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run_command(command, capture_output=False, fail_on_error=True) -> str | None:
    """Run a shell command.

    Args:
        command: The shell command to run.
        capture_output: Whether to capture the command's output.
        fail_on_error: Whether to exit the script if the command fails.

    Returns:
        The command's output if `capture_output` is `True`, otherwise `None`.
    """
    try:
        result = subprocess.run(
            command, shell=True, check=True, text=True, capture_output=capture_output
        )
        return result.stdout.strip() if capture_output else None
    except subprocess.CalledProcessError as e:
        if fail_on_error:
            print(f"Error running command: {command}\n{e}")
            sys.exit(1)
        return None


def ensure_terraform(expected_version) -> None:
    """Ensure Terraform is installed and matches the expected version.

    Args:
        expected_version: The expected version of Terraform.
    """
    installed_version = run_command(
        "snap list terraform | awk '/^terraform/ {print $4}'", capture_output=True
    )
    if not installed_version:
        print(f"Terraform is not installed. Installing version {expected_version}...")
        run_command(f"sudo snap install terraform --channel={expected_version} --classic")
    elif installed_version != expected_version:
        print(
            f"Error: Installed Terraform version ({installed_version})"
            f"does not match the expected version ({expected_version})."
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
            f"juju show-controller | yq .{controller}.details.api-endpoints"
            "| yq -r '. | join(\",\")'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_USERNAME"] = (
        run_command(
            f"cat ~/.local/share/juju/accounts.yaml"
            f"| yq .controllers.{controller}.user | tr -d '\"'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_PASSWORD"] = (
        run_command(
            f"cat ~/.local/share/juju/accounts.yaml"
            f"| yq .controllers.{controller}.password | tr -d '\"'",
            capture_output=True,
        )
        or ""
    )
    os.environ["JUJU_CA_CERT"] = (
        run_command(
            f"juju show-controller {controller.strip()}"
            f"| yq '.{controller}.details.\"ca-cert\"' | tr -d '\"' | sed 's/\\\\n/\\n/g'",
            capture_output=True,
        )
        or ""
    )


def ensure_model_exists(model_name, lxd_profile_path):
    """Ensure the specified Juju model exists.

    If not, create it and configure LXD profile if required.

    Args:
        model_name: The name of the Juju model.
        lxd_profile_path: The path to the LXD profile file.
    """
    # Grep returns 1 if no match is found, so we use `fail_on_error=False` to suppress the error
    if run_command(
        f"juju models | grep -q {model_name}", capture_output=True, fail_on_error=False
    ):
        print(f"Juju model '{model_name}' already exists.")
    else:
        print(f"Juju model '{model_name}' does not exist. Creating it...")
        run_command(f"juju add-model {model_name} localhost")

    controller_cloud = run_command(
        "juju show-controller | yq -r .$CONTROLLER.details.cloud", capture_output=True
    )
    if controller_cloud in ["localhost", "lxd"]:
        print(
            "Current Juju controller is using LXD/localhost. Applying 'k8s.profile' to the model..."
        )
        run_command(f"lxc profile edit juju-{model_name} < {lxd_profile_path}")
    else:
        print("Current Juju controller is not LXD/localhost. Skipping 'k8s.profile' application.")


def main():
    """Main entrypoint."""
    script_dir = Path(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser(description="Terraform and Juju setup script.")
    parser.add_argument(
        "--terraform-version", default="latest/stable", help="Expected Terraform version."
    )
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
        default=script_dir / "data" / "default-manifest.yaml",
        help="Path to the manifest YAML file.",
    )
    parser.add_argument("--model-name", default="my-canonical-k8s", help="Name of the Juju model.")

    args = parser.parse_args()

    # Install or validate Terraform
    ensure_terraform(args.terraform_version)

    # Set up Juju provider authentication
    setup_juju_provider_authentication()

    # Ensure the Juju model exists
    ensure_model_exists(args.model_name, args.lxd_profile_path)

    # Change to the Terraform module directory
    os.chdir(args.terraform_module_path)

    # Run Terraform commands
    print("Running 'terraform init'")
    run_command("terraform init")

    print(
        f"Running 'terraform plan' with manifest: {args.manifest_path} "
        f"and model: {args.model_name}..."
    )
    run_command(
        f"terraform plan -var='manifest_path={args.manifest_path}' "
        f"-var='model_name={args.model_name}'"
    )

    print(
        f"Running 'terraform apply' with manifest: {args.manifest_path} "
        f"and model: {args.model_name}..."
    )
    run_command(
        f"terraform apply -var='manifest_path={args.manifest_path}' "
        f"-var='model_name={args.model_name}' -auto-approve"
    )


if __name__ == "__main__":
    main()
