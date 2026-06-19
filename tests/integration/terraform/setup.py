#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Deploy a Kubernetes cluster using the Juju Terraform provider and a manifest as input.

See https://github.com/canonical/k8s-bundles/blob/main/terraform/README.md
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


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
            [
                "sudo",
                "snap",
                "install",
                "terraform",
                "--channel",
                expected_version,
                "--classic",
            ]
        )
    elif installed_version != expected_version:
        print(
            f"Error: Installed Terraform ({installed_version}) does \
                not match expected ({expected_version})."
        )
        sys.exit(1)
    else:
        print(
            "Terraform is already installed and "
            f"matches the expected version: {installed_version}."
        )


def setup_terraform_env(args) -> None:
    """Retrieve Juju provider authentication details using the Juju CLI."""
    out = run_command(
        ["juju", "show-controller", "--show-password", "--format", "json"],
        capture_output=True,
    )
    data = json.loads(out or "{}")
    controller_name, info = next(iter(data.items()))
    details = info.get("details", {})
    account = info.get("account", {})

    # Set environment variables
    os.environ.update(
        {
            "CONTROLLER": controller_name or "",
            "JUJU_CONTROLLER_ADDRESSES": ",".join(details.get("api-endpoints", [])),
            "JUJU_USERNAME": account.get("user", "") or "",
            "JUJU_PASSWORD": account.get("password", "") or "",
            "JUJU_CA_CERT": details.get("ca-cert", "") or "",
            "TF_VAR_cloud": details.get("cloud", "") or "",
            "TF_VAR_model": args.model,
            "TF_VAR_manifest_yaml": str(args.manifest_yaml.absolute()),
        }
    )


def model_exists(model: str) -> bool:
    """Check if the model already exists in the controller."""
    out = run_command(["juju", "models", "--format", "json"], capture_output=True)
    data = json.loads(out or "{}")
    names = set()
    for m in data.get("models", []):
        names.add(m.get("name"))
        names.add(m.get("short-name"))
    return model in names


def tf_run(path: Path, args: List[str]) -> None:
    """Run Terraform command."""
    current = os.getcwd()
    try:
        os.chdir(path)
        run_command(["terraform"] + args)
    finally:
        os.chdir(current)


def main() -> None:
    """Set up entry point for Terraform and Juju."""
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(description="Terraform and Juju setup script.")
    parser.add_argument(
        "--terraform-version",
        default="latest/stable",
        help="Expected Terraform version.",
    )
    parser.add_argument(
        "--terraform-module-path", default=script_dir, help="Path to Terraform modules."
    )
    parser.add_argument(
        "--manifest-yaml",
        default=script_dir / "k8s-manifest.yaml",
        type=Path,
        help="Path to manifest.",
    )
    parser.add_argument("--model", default="my-canonical-k8s", help="Juju model name.")

    args = parser.parse_args()

    ensure_terraform(args.terraform_version)

    # Set up Juju auth details
    setup_terraform_env(args)

    # Run Terraform commands
    print("Initializing Terraform...")
    tf_run(args.terraform_module_path, ["init", "--upgrade"])

    # Import Existing Model if it exists
    if model_exists(args.model):
        print("Import existing model...")
        tf_run(
            args.terraform_module_path,
            ["import", "module.k8s.juju_model.this", args.model],
        )

    print(f"Applying Terraform with manifest: {args.manifest_yaml} and model: {args.model}...")
    print(os.environ.get("TF_VAR_csi_integration"))
    tf_run(
        args.terraform_module_path,
        [
            "apply",
            "-auto-approve",
        ],
    )


if __name__ == "__main__":
    main()
