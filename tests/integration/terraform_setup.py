#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""
Deploy a Kubernetes cluster using the Juju Terraform provider and a manifest as input.
See https://github.com/canonical/k8s-bundles/blob/main/terraform/README.md
"""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Union

import yaml
from juju.controller import Controller


def run_command(
    command: List[str], capture_output: bool = False, fail_on_error: bool = True
) -> Optional[str]:
    """Run a shell command safely.

    Args:
        command: List of command arguments.
        capture_output: Whether to capture the command's output.
        fail_on_error: Whether to exit the script if the command fails.

    Returns:
        The command's output if `capture_output` is `True`, otherwise `None`.
    """
    try:
        result = subprocess.run(command, check=True, text=True, capture_output=capture_output)
        return result.stdout.strip() if capture_output else None
    except subprocess.CalledProcessError as e:
        if fail_on_error:
            print(f"Error running command: {' '.join(command)}\n{e.stderr or e}")
            sys.exit(1)
        return None


def get_installed_terraform_channel() -> Optional[str]:
    """Retrieve the installed Terraform channel from the snap list output.

    Returns:
        The installed Terraform channel, or `None` if Terraform is not installed
    """
    try:
        output = run_command(
            ["snap", "list", "terraform"], capture_output=True, fail_on_error=False
        )
        if output:
            for line in output.splitlines():
                parts = line.split()
                if parts and parts[0] == "terraform":
                    return parts[3]  # Fourth column contains the channel
    except subprocess.CalledProcessError as e:
        print(f"Error retrieving Terraform version: {e}")
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
        run_command(
            ["sudo", "snap", "install", "terraform", "--channel", expected_version, "--classic"]
        )
    elif installed_version != expected_version:
        print(
            f"Error: Installed Terraform ({installed_version}) does \
                not match expected ({expected_version})."
        )
        sys.exit(1)
    else:
        print(
            f"Terraform is already installed and matches the expected version: {installed_version}."
        )


async def setup_juju_auth_details() -> None:
    """Retrieve Juju provider authentication details using the Juju Python library."""
    async with Controller() as controller:
        await controller.connect_current()
        controller_name = controller.controller_name
        controller_info = (await controller.info()).results[0]
        user = await controller.get_current_user()
        endpoints = await controller.api_endpoints

        # Read password from accounts.yaml as the Juju
        # API does not expose the password for security reasons.
        accounts_file = Path.home() / ".local/share/juju/accounts.yaml"
        password = ""
        if accounts_file.exists():
            with open(accounts_file, "r", encoding="utf-8") as f:
                accounts_data = yaml.safe_load(f)
                password = (
                    accounts_data.get("controllers", {})
                    .get(controller_name, {})
                    .get("password", "")
                )

        # Set environment variables
        os.environ.update(
            {
                "CONTROLLER": controller_name or "",
                "JUJU_CONTROLLER_ADDRESSES": ",".join(endpoints) or "",
                "JUJU_USERNAME": user.username or "",
                "JUJU_PASSWORD": password or "",
                "JUJU_CA_CERT": controller_info.cacert or "",
            }
        )


async def ensure_model_exists(model_name: str, lxd_profile_path: Union[Path, str]) -> None:
    """Ensure the specified Juju model exists, creating it if necessary.

    Args:
        model_name: The name of the Juju model.
        lxd_profile_path: The path to the LXD profile file.
    """
    async with Controller() as controller:
        await controller.connect_current()
        model_names = await controller.list_models()

        if model_name in model_names:
            print(f"Juju model '{model_name}' already exists.")
        else:
            print(f"Juju model '{model_name}' does not exist. Creating it...")
            await controller.add_model(model_name)

        controller_cloud = (await controller.cloud()).cloud
        print(f"Controller cloud: {controller_cloud}")

        if controller_cloud.type_ == "lxd":
            print("Applying 'k8s.profile' to the model...")
            subprocess.run(
                ["lxc", "profile", "edit", f"juju-{model_name}"],
                check=True,
                capture_output=True,
                input=Path(lxd_profile_path).read_bytes(),
            )
        else:
            print("Skipping 'k8s.profile' application (not LXD/localhost).")


async def main() -> None:
    """Main entry point for setting up Terraform and Juju."""
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description="Terraform and Juju setup script.")
    parser.add_argument(
        "--terraform-version", default="latest/stable", help="Expected Terraform version."
    )
    parser.add_argument(
        "--terraform-module-path", default=script_dir / "data", help="Path to Terraform module."
    )
    parser.add_argument(
        "--lxd-profile-path", default=script_dir / "data/k8s.profile", help="Path to LXD profile."
    )
    parser.add_argument(
        "--manifest-path",
        default=script_dir / "data/default-manifest.yaml",
        help="Path to manifest.",
    )
    parser.add_argument("--model-name", default="my-canonical-k8s", help="Juju model name.")

    args = parser.parse_args()

    ensure_terraform(args.terraform_version)

    # Set up Juju and ensure the model exists
    await asyncio.gather(
        setup_juju_auth_details(),
        ensure_model_exists(args.model_name, args.lxd_profile_path),
    )

    # Change to Terraform module directory
    os.chdir(args.terraform_module_path)

    # Run Terraform commands
    print("Initializing Terraform...")
    run_command(["terraform", "init"])

    print(
        f"Applying Terraform with manifest: {args.manifest_path} and model: {args.model_name}..."
    )
    run_command(
        [
            "terraform",
            "apply",
            "-var",
            f"manifest_path={args.manifest_path}",
            "-var",
            f"model_name={args.model_name}",
            "-auto-approve",
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())
