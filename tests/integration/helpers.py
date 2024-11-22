# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Additions to tools missing from juju library."""

# pylint: disable=too-many-arguments,too-many-positional-arguments

import ipaddress
import json
import logging
import shlex
from pathlib import Path
from typing import List

import yaml
from juju import unit
from juju.model import Model
from tenacity import AsyncRetrying, before_sleep_log, retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


async def is_deployed(model: Model, bundle_path: Path) -> bool:
    """Checks if model has apps defined by the bundle.
    If all apps are deployed, wait for model to be active/idle

    Args:
        model: juju model
        bundle_path: path to bundle for comparison

    Returns:
        true if all apps and relations are in place and units are active/idle
    """
    bundle = yaml.safe_load(bundle_path.open())
    apps = bundle["applications"]
    for app, conf in apps.items():
        if app not in model.applications:
            log.warning(
                "Cannot use existing model(%s): Application (%s) isn't deployed", model.name, app
            )
            return False
        min_units = conf.get("num_units") or 1
        num_units = len(model.applications[app].units)
        if num_units < min_units:
            log.warning(
                "Cannot use existing model(%s): Application(%s) has insufficient units %d < %d",
                model.name,
                app,
                num_units,
                min_units,
            )
            return False
    await model.wait_for_idle(status="active", timeout=20 * 60, raise_on_error=False)
    return True


async def get_unit_cidrs(model: Model, app_name: str, unit_num: int) -> List[str]:
    """Find unit network cidrs on a unit.

    Args:
        model: juju model
        app_name: string name of application
        unit_num: integer number of a juju unit

    Returns:
        list of network cidrs
    """
    unit = model.applications[app_name].units[unit_num]
    action = await unit.run("ip --json route show")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get routes"
    routes = json.loads(result.results["stdout"])
    local_cidrs = set()
    for rt in routes:
        try:
            cidr = ipaddress.ip_network(rt.get("dst"))
        except ValueError:
            continue
        if cidr.prefixlen < 32:
            local_cidrs.add(str(cidr))
    return list(sorted(local_cidrs))


async def get_nodes(k8s):
    """Return Node list

    Args:
        k8s: any k8s unit

    Returns:
        list of nodes
    """
    action = await k8s.run("k8s kubectl get nodes -o json")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get nodes with kubectl"
    log.info("Parsing node list...")
    node_list = json.loads(result.results["stdout"])
    assert node_list["kind"] == "List", "Should have found a list of nodes"
    return node_list["items"]


@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(15))
async def ready_nodes(k8s, expected_count):
    """Get a list of the ready nodes.

    Args:
        k8s: k8s unit
        expected_count: number of expected nodes
    """
    log.info("Finding all nodes...")
    nodes = await get_nodes(k8s)
    ready_nodes = {
        node["metadata"]["name"]: all(
            condition["status"] == "False"
            for condition in node["status"]["conditions"]
            if condition["type"] != "Ready"
        )
        for node in nodes
    }
    log.info("Found %d/%d nodes...", len(ready_nodes), expected_count)
    assert len(ready_nodes) == expected_count, f"Expect {expected_count} nodes in the list"
    for node, ready in ready_nodes.items():
        log.info("Node %s is %s..", node, "ready" if ready else "not ready")
        assert ready, f"Node not yet ready: {node}."


async def wait_pod_phase(
    k8s: unit.Unit,
    name: str,
    *phase: str,
    namespace: str = "default",
    retry_times: int = 30,
    retry_delay_s: int = 15,
):
    """Wait for the pod to reach the specified phase (e.g. Succeeded).

    Args:
        k8s: k8s unit
        name: the pod name
        phase: expected phase
        namespace: pod namespace
        retry_times: the number of retries
        retry_delay_s: retry interval

    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(retry_times),
        wait=wait_fixed(retry_delay_s),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            cmd = shlex.join(
                [
                    "k8s",
                    "kubectl",
                    "get",
                    "--namespace",
                    namespace,
                    "-o",
                    "jsonpath={.status.phase}",
                    f"pod/{name}",
                ]
            )
            action = await k8s.run(cmd)
            result = await action.wait()
            stdout, stderr = (
                result.results.get(field, "").strip() for field in ["stdout", "stderr"]
            )
            assert result.results["return-code"] == 0, (
                f"\nPod hasn't reached phase: {phase}\n"
                f"\tstdout: '{stdout}'\n"
                f"\tstderr: '{stderr}'"
            )
            assert stdout in phase, f"Pod {name} not yet in phase {phase} ({stdout})"


async def get_pod_logs(
    k8s: unit.Unit,
    name: str,
    namespace: str = "default",
) -> str:
    """Retrieve pod logs.

    Args:
        k8s: k8s unit
        name: pod name
        namespace: pod namespace

    Returns:
        the pod logs as string.
    """
    cmd = " ".join(["k8s kubectl logs", f"--namespace {namespace}", f"pod/{name}"])
    action = await k8s.run(cmd)
    result = await action.wait()
    assert result.results["return-code"] == 0, f"Failed to retrieve pod {name} logs."
    return result.results["stdout"]
