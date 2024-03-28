# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Sync AlertManager rules from an upstream repository.

This script fetches AlertManager rules definitions from a specified version
of the kube-prometheus project and adjusts them for compatibility with
COS.
"""

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen

import yaml

logging.basicConfig(level=logging.INFO)

# NOTE: pick a kube-prometheus version that supports the Kubernetes version we deploy
VERSION = "v0.13.0"
SOURCE = (
    f"https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/{VERSION}/manifests"
)
RULE_FILES = [
    "kubePrometheus-prometheusRule.yaml",
    "kubeStateMetrics-prometheusRule.yaml",
    "kubernetesControlPlane-prometheusRule.yaml",
]
ALERT_RULES_DIR = Path("src/prometheus_alert_rules")
PATCHES_DIR = Path("scripts/rules-patches")

DROP_RECORDS = [
    ("kube-apiserver-availability.rules", "code_verb:apiserver_request_total:increase1h")
]


def download_and_process_rule_files(temp_dir: Path):
    """Download Prometheus rule files from the specified SOURCE and process them.

    This includes filtering out specified records from HACK_DROP_RECORDS and writing the
    processed rules to files in the ALERT_RULES_DIR directory.

    Args:
        temp_dir (Path): temporary directory
    """
    for file in RULE_FILES:
        source_url = f"{SOURCE}/{file}"
        temp_file = temp_dir / file
        try:
            logging.info(f"Downloading {source_url}")
            with urlopen(source_url) as response:  # nosec
                process_rule_file(response, temp_file, source_url)
        except URLError as e:
            logging.error(f"Error fetching dashboard data: {e}")


def process_rule_file(contents, destination_file: Path, source_url: str):
    """Process a single Prometheus rule file's contents.

    This function also filters out unwanted records and writes the processed rules
    to a new file.

    Args:
        contents (str): The raw contents of the rule file.
        destination_file (Path): The path to the file where the processed rules will be saved.
        source_url (str): The URL from which the original rule file was downloaded.
    """
    alert_rules = yaml.safe_load(contents)["spec"]

    for group in alert_rules["groups"]:
        group["rules"] = [
            rule
            for rule in group["rules"]
            if (group["name"], rule.get("record")) not in DROP_RECORDS
        ]

    data = [
        "# Copyright 2024 Canonical Ltd.",
        "# See LICENSE file for licensing details.\n\n" f"# Automatically generated by {sys.argv}",
        f"# Source: {source_url}",
        yaml.safe_dump(alert_rules),
    ]

    with destination_file.open("w") as file:
        file.write("\n".join(data))
    logging.info(f"Processed and saved to {destination_file}")


def move_processed_files(temp_dir):
    """Move the processed rule files from the temporary directory.

    Args:
        temp_dir (Path): The temporary directory from which files will be moved.
    """
    for temp_file in temp_dir.iterdir():
        final_path = ALERT_RULES_DIR / temp_file.name
        shutil.move(str(temp_file), str(final_path))
        logging.info(f"Moved {temp_file.name} to {final_path}")


def apply_patches():
    """Apply patches to the downloaded and processed rule files."""
    for patch_file in PATCHES_DIR.glob("*"):
        logging.info(f"Applying patch {patch_file}")
        subprocess.check_call(["/usr/bin/git", "apply", str(patch_file)])


def main():
    """Fetch, process, and save AlertManager rules."""
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        try:
            download_and_process_rule_files(temp_path)
            shutil.rmtree(ALERT_RULES_DIR, ignore_errors=True)
            ALERT_RULES_DIR.mkdir(parents=True)
            move_processed_files(temp_path)
            apply_patches()
        except Exception as e:
            logging.error("An error occurred: %s" % e)


if __name__ == "__main__":
    main()
