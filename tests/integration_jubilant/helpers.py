# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Sync helper utilities for Jubilant-based integration tests."""

import json
import logging
from typing import Any, Dict, List, Optional

import jubilant
from tenacity import (
    Retrying,
    before_sleep_log,
    stop_after_attempt,
    wait_fixed,
)

log = logging.getLogger(__name__)


def get_rsc(
    juju: jubilant.Juju,
    unit: str,
    resource: str,
    namespace: Optional[str] = None,
    labels: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Get a kubectl resource list, optionally filtered by namespace and labels.

    Args:
        juju:      Jubilant Juju instance.
        unit:      Unit name to run kubectl on (e.g. "k8s/0").
        resource:  Resource type (e.g. "pods", "nodes").
        namespace: Optional namespace filter.
        labels:    Optional label filter dict.

    Returns:
        List of resource dicts from the JSON output.
    """
    namespaced = f"-n {namespace}" if namespace else ""
    labeled = " ".join(f"-l {k}={v}" for k, v in labels.items()) if labels else ""
    cmd = f"k8s kubectl get {resource} {labeled} {namespaced} -o json"

    task = juju.exec(cmd, unit=unit)
    stdout = task.stdout.strip() if task.stdout else ""
    log.info("Parsing %s list...", resource)
    resource_obj = json.loads(stdout)
    if "/" in resource:
        return [resource_obj]
    assert resource_obj["kind"] == "List", f"Should have found a list of {resource}"
    return resource_obj["items"]


def ready_nodes(juju: jubilant.Juju, unit: str, expected_count: int) -> None:
    """Assert that exactly *expected_count* nodes are ready.

    Retries up to 12 times with a 15-second wait between attempts.

    Args:
        juju:           Jubilant Juju instance.
        unit:           k8s unit name to run kubectl on (e.g. "k8s/0").
        expected_count: Number of expected ready nodes.

    Raises:
        AssertionError: if the node count or readiness assertion fails after all retries.
        RetryError:     propagated from tenacity if all attempts fail.
    """
    for attempt in Retrying(
        reraise=True,
        stop=stop_after_attempt(12),
        wait=wait_fixed(15),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            log.info("Finding all nodes...")
            nodes = get_rsc(juju, unit, "nodes")
            node_readiness = {
                node["metadata"]["name"]: all(
                    condition["status"] == "False"
                    for condition in node["status"]["conditions"]
                    if condition["type"] != "Ready"
                )
                for node in nodes
            }
            log.info("Found %d/%d nodes...", len(node_readiness), expected_count)
            assert len(node_readiness) == expected_count, (
                f"Expect {expected_count} nodes in the list"
            )
            for node, is_ready in node_readiness.items():
                log.info("Node %s is %s..", node, "ready" if is_ready else "not ready")
                assert is_ready, f"Node not yet ready: {node}."


def wait_pod_phase(
    juju: jubilant.Juju,
    unit: str,
    name: Optional[str],
    *phase: str,
    namespace: str = "default",
    retry_times: int = 30,
    retry_delay_s: int = 15,
) -> None:
    """Wait for pods to reach the specified phase (e.g. "Running").

    Args:
        juju:         Jubilant Juju instance.
        unit:         k8s unit name to run kubectl on (e.g. "k8s/0").
        name:         Pod name, or None to check all pods in the namespace.
        *phase:       Expected phase(s).
        namespace:    Pod namespace.
        retry_times:  Number of retries.
        retry_delay_s: Seconds to wait between retries.
    """
    pod_resource = "pod" if name is None else f"pod/{name}"
    for attempt in Retrying(
        reraise=True,
        stop=stop_after_attempt(retry_times),
        wait=wait_fixed(retry_delay_s),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            for pod in get_rsc(juju, unit, pod_resource, namespace=namespace):
                _phase = pod["status"]["phase"]
                _name = pod["metadata"]["name"]
                assert _phase in phase, f"Pod {_name} not yet in phase {phase}"


def get_leader(juju: jubilant.Juju, app: str) -> str:
    """Return the name of the leader unit for *app*.

    Args:
        juju: Jubilant Juju instance.
        app:  Application name (e.g. "k8s").

    Returns:
        Leader unit name (e.g. "k8s/0").

    Raises:
        ValueError: if no leader is found.
    """
    status = juju.status()
    for unit_name, unit_status in status.apps[app].units.items():
        if unit_status.leader:
            return unit_name
    raise ValueError(f"No leader found for app '{app}'")


def get_unit_names(juju: jubilant.Juju, app: str) -> List[str]:
    """Return a sorted list of all unit names for *app*.

    Args:
        juju: Jubilant Juju instance.
        app:  Application name.

    Returns:
        Sorted list of unit names (e.g. ["k8s/0", "k8s/1"]).
    """
    return sorted(juju.status().apps[app].units)
