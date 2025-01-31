#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""
This script deploys a Kubernetes cluster using the Juju
Terraform provider and a manifest as input.
See https://github.com/canonical/k8s-bundles/blob/main/terraform/README.md
"""

import argparse
import os
import subprocess
import sys
from  juju.controller import Controller
import asyncio
from pathlib import Path
from typing import Optional, Union, List


def run_command(
    command: List[str], capture_output: bool = False, fail_on_error: bool = True
) -> Optional[str]:
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
            command, check=True, text=True, capture_output=capture_output
        )
        return result.stdout.strip() if capture_output else None
    except subprocess.CalledProcessError as e:
        if fail_on_error:
            print(f"Error running command: {command}\n{e}")
            sys.exit(1)
        return None

def get_installed_terraform_channel() -> Optional[str]:
    """Retrieve the installed Terraform channel from the snap list output."""
    result = None
    try:
        result = subprocess.run(
            ["snap", "list", "terraform"], check=True, text=True, capture_output=True
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == "terraform":
                return parts[3]  # The fourth column contains the channel
    except subprocess.CalledProcessError:
        if result:
            print("Error retrieving Terraform version: {}".format(result.stderr))
            sys.exit(1)

    return None

def ensure_terraform(expected_version: str) -> None:
    """Ensure Terraform is installed and matches the expected version.

    Args:
        expected_version: The expected version of Terraform.
    """
    installed_version = get_installed_terraform_channel()
    if not installed_version:
        print(f"Terraform is not installed. Installing version {expected_version}...")
        run_command(f"sudo snap install terraform --channel {expected_version} --classic".split())
    elif installed_version != expected_version:
        print(
            f"Error: Installed Terraform channel ({installed_version})"
            f"does not match the expected channel ({expected_version})."
        )
        sys.exit(1)
    else:
        print(
            f"Terraform is already installed and matches the expected version: {installed_version}."
        )


async def setup_juju_auth_details():
    """Retrieve Juju provider authentication details using the Juju Python library."""
    controller = Controller()
    try:
        await controller.connect()  # Connect to the current controller
        controller_name = controller.controller_name

        # Fetch controller details
        controller_info = await controller.get_controller()
        accounts = controller.accounts[controller_name]

        # Extract relevant details
        os.environ["CONTROLLER"] = controller_name or ""
        os.environ["JUJU_CONTROLLER_ADDRESSES"] = ",".join(controller_info.api_endpoints) or ""
        os.environ["JUJU_USERNAME"] = accounts["user"] or ""
        os.environ["JUJU_PASSWORD"] = accounts["password"] or ""
        os.environ["JUJU_CA_CERT"] = controller_info.ca_certificate or ""

    finally:
        await controller.disconnect()


async def ensure_model_exists(model_name: str, lxd_profile_path: Union[Path, str]) -> None:
    """Ensure the specified Juju model exists.

    If not, create it and configure LXD profile if required.

    Args:
        model_name: The name of the Juju model.
        lxd_profile_path: The path to the LXD profile file.
    """
    controller = Controller()
    try:
        await controller.connect()
        model_names = await controller.list_models()

        if model_name in model_names:
            print(f"Juju model '{model_name}' already exists.")
        else:
            print(f"Juju model '{model_name}' does not exist. Creating it...")
            await controller.add_model(model_name)

        # Get the controller cloud
        controller_info = await controller.get_controller()
        controller_cloud = controller_info.cloud
        print(f"Controller cloud: {controller_cloud}")

    finally:
        await controller.disconnect()
    if controller_cloud in ["localhost", "lxd"]:
        print(
            "Current Juju controller is using LXD/localhost. Applying 'k8s.profile' to the model..."
        )
        subprocess.run(
            f"lxc profile edit juju-{model_name}".split(), check=True, text=True, capture_output=True, input=Path(lxd_profile_path).read_text()
        )
    else:
        print("Current Juju controller is not LXD/localhost. Skipping 'k8s.profile' application.")


async def main():
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
    await setup_juju_auth_details()

    # Ensure the Juju model exists
    await ensure_model_exists(args.model_name, args.lxd_profile_path)

    # Change to the Terraform module directory
    os.chdir(args.terraform_module_path)

    # Run Terraform commands
    print("Running 'terraform init'")
    run_command(["terraform", "init"])

    print(
        f"Running 'terraform apply' with manifest: {args.manifest_path} "
        f"and model: {args.model_name}..."
    )
    run_command(
        ["terraform", "apply", "-var", "manifest_path={args.manifest_path}", "-var", "model_name={args.model_name}", "-auto-approve"]
    )


if __name__ == "__main__":
    asyncio.run(main())
