# Copyright 2024 Canonical Ltd.
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
from urllib.error import URLError
from urllib.request import urlopen

import yaml

logging.basicConfig(level=logging.INFO)

VERSION = "v0.13.0"
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
        logging.error(f"Error fetching dashboard data: {e}")
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
    return json.dumps(json_value, indent=4).replace("$datasource", "$prometheusds")


def save_dashboard_to_file(name, data):
    """Save the prepared dashboard JSON to a file.

    Args:
        name (str): name of the dashboard file
        data (str): file content to write
    """
    filepath = os.path.join(TARGET_DIR, name)
    with open(filepath, "w") as f:
        f.write(data)
    logging.info(f"Dashboard '{name}' saved to {filepath}")


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
    else:
        logging.info("No data fetched. Exiting.")


if __name__ == "__main__":
    main()
