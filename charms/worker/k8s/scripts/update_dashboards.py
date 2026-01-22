# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Sync Grafana dashboards from an upstream repository.

This script fetches Grafana dashboard definitions from a specified version
of the kube-prometheus project and adjusts them for compatibility with
COS by removing the built-in $prometheus datasource.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

logging.basicConfig(level=logging.INFO)

VERSION = "v0.15.0"
SOURCE_URL = (
    "https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/"
    f"{VERSION}/manifests/grafana-dashboardDefinitions.yaml"
)
DASHBOARDS = {
    "apiserver.json",
    "cluster-total.json",
    "controller-manager.json",
    "k8s-resources-cluster.json",
    "k8s-resources-multicluster.json",
    "k8s-resources-namespace.json",
    "k8s-resources-node.json",
    "k8s-resources-pod.json",
    "k8s-resources-workload.json",
    "k8s-resources-workloads-namespace.json",
    "kubelet.json",
    "namespace-by-pod.json",
    "namespace-by-workload.json",
    "persistentvolumesusage.json",
    "pod-total.json",
    "proxy.json",
    "scheduler.json",
    "workload-total.json",
}
TARGET_DIR = "src/grafana_dashboards"
PATCHES_DIR = Path("scripts/dashboard-patches")


def apply_patches():
    """Apply patches to the downloaded and processed dashboard files.

    The following patches are applied to the upstream dashboards:

        001_node_memory_job: The patch changes the job filter
            to a regular expression to match any job ending with
            "node-exporter".
    """
    for patch_file in PATCHES_DIR.glob("*"):
        logging.info("Applying patch %s", patch_file)
        subprocess.check_call(["/usr/bin/git", "apply", str(patch_file)])


def fetch_dashboards(source_url: str):
    """Fetch and load dashboard definitions from the specified URL.

    Args:
        source_url (str): URL to dashboards

    Returns:
        Parsed yaml dashboard content
    """
    try:
        with urlopen(source_url) as response:  # nosec
            return yaml.safe_load(response.read())
    except URLError as e:
        logging.error("Error fetching dashboard data: %s", e)
        return None


def dashboards_data(data):
    """Yield dashboard data for dashboards specified in DASHBOARDS.

    Args:
        data (dict): data containing dashboard data.

    Yields:
        Tuple[str, Any]: key and values from the dashboard data
    """
    if not data:
        return

    for config_map in data["items"]:
        for key, value in config_map["data"].items():
            if key in DASHBOARDS:
                yield key, json.loads(value)


def prepare_dashboard(json_value):
    """Prepare dashboard data for COS integration.

    removes the built-in Prometheus datasource

    Args:
        json_value (dict): updated templating dashboard data

    Returns:
        string formatted dashboard
    """
    json_value["templating"]["list"] = [
        item
        for item in json_value.get("templating", {}).get("list", [])
        if not (item.get("name") == "datasource" and item.get("type") == "datasource")
    ]
    return (
        json.dumps(json_value, indent=4)
        .replace("$datasource", "$prometheusds")
        .replace("${datasource}", "${prometheusds}")
    )


def save_dashboard_to_file(name, data: str):
    """Save the prepared dashboard JSON to a file.

    Args:
        name (str): name of the dashboard file
        data (str): file content to write
    """
    filepath = os.path.join(TARGET_DIR, name)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(data)
    logging.info("Dashboard '%s' saved to %s", name, filepath)


def main():
    """Fetch, process, and save Grafana dashboards."""
    if os.path.exists(TARGET_DIR):
        shutil.rmtree(TARGET_DIR)
    os.makedirs(TARGET_DIR, exist_ok=True)

    dashboards = fetch_dashboards(SOURCE_URL)
    if dashboards:
        for name, data in dashboards_data(dashboards):
            dashboard = prepare_dashboard(data)
            save_dashboard_to_file(name, dashboard)
        apply_patches()
    else:
        logging.info("No data fetched. Exiting.")


if __name__ == "__main__":
    main()
