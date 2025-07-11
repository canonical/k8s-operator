# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Sync AlertManager rules from an upstream repository.

This script fetches AlertManager rules definitions from a specified version
of the kube-prometheus project and adjusts them for compatibility with
COS.
"""

import logging
import os
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
# As of 2025-06-16, v0.15.0 supports 1.31-1.33.
VERSION = "v0.15.0"
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

# NOTE: (mateoflorido): This record is duplicated across the rules. As of
# 2025-06-16 (v0.15.0), Prometheus does not support duplicated records.
DROP_RECORDS = [
    ("kube-apiserver-availability.rules", "code_verb:apiserver_request_total:increase1h")
]


def str_presenter(dumper, data):
    """Serialize multiline YAML strings."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class MultiLineDumper(yaml.SafeDumper):
    """A SafeDumper class that handles multiline strings."""

    pass


MultiLineDumper.add_representer(str, str_presenter)


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
            logging.info("Downloading %s", source_url)
            with urlopen(source_url) as response:  # nosec
                process_rule_file(response, temp_file, source_url)
        except URLError as e:
            logging.error("Error fetching dashboard data: %s", e)


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
    filename = os.path.basename(sys.argv[0])

    for group in alert_rules["groups"]:
        group["rules"] = [
            rule
            for rule in group["rules"]
            if (group["name"], rule.get("record")) not in DROP_RECORDS
        ]

    data = [
        "# Copyright 2025 Canonical Ltd.",
        f"# See LICENSE file for licensing details.\n\n# Automatically generated by {filename}",
        f"# Source: {source_url}",
        yaml.dump(alert_rules, Dumper=MultiLineDumper),
    ]

    with destination_file.open("w") as file:
        file.write("\n".join(data))
    logging.info("Processed and saved to %s", destination_file)


def move_processed_files(temp_dir):
    """Move the processed rule files from the temporary directory.

    Args:
        temp_dir (Path): The temporary directory from which files will be moved.
    """
    for temp_file in temp_dir.iterdir():
        final_path = ALERT_RULES_DIR / temp_file.name
        shutil.move(str(temp_file), str(final_path))
        logging.info("Moved %s to %s", temp_file.name, final_path)


def apply_patches():
    """Apply patches to the downloaded and processed rule files.

    The following patches are applied to the upstream rules:
        001_core_alert_rules: Modifies alerting rules for core K8s components
            The original rules use the absent() function to alert when a
            target is missing from Prometheus. In our environment,
            targets are always present, but their health is indicated by the
            "up" metric. This patch changes the alert expressions to trigger
            when up == 0.
        002_cpu_utilization: Updates the CPU utilization recording rule to
            support charmed environments where the node-exporter job name may
            differ from the one used upstream. The patch changes the job filter
            to a regular expression to match any job ending with
            "node-exporter", and uses `label_replace` to extract the cluster
            name from the Juju model.
            It also adjusts the aggregation and joins with `kube_node_info`
            to ensure the resulting metric is compatible with the expected
            node labels.
        003_mem_node_exporter: Similar to 002, this patch applies
            `label_replace` to ensure compatibility with expected labels.
        004_node_network_iface: Similar to 002, this patch adjusts the network
            interface recording rules to match the actual job and label
            conventions  used in charmed environments,
        005_num_cpu: Similar to 002. Updates the CPU count recording rule
            to support charmed environments The patch adjusts the join logic
            to use the instance label instead of node. It also uses
            `label_replace` to align labels between metrics.
    """
    for patch_file in PATCHES_DIR.glob("*"):
        logging.info("Applying patch %s", patch_file)
        subprocess.check_call(["/usr/bin/git", "apply", str(patch_file)])


def main():
    """Fetch, process, and save AlertManager rules."""
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        download_and_process_rule_files(temp_path)
        shutil.rmtree(ALERT_RULES_DIR, ignore_errors=True)
        ALERT_RULES_DIR.mkdir(parents=True)
        move_processed_files(temp_path)
        apply_patches()


if __name__ == "__main__":
    main()
