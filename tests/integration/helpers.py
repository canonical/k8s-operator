# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Synchronous helpers for driving a deployed Canonical Kubernetes cluster with Jubilant."""

import contextlib
import ipaddress
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import jubilant
from literals import DEFAULT_DELAY, DEFAULT_SUCCESSES
from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


def render_dir() -> Path:
    """Return a writable directory whose contents the juju CLI can read.

    When juju is installed as a snap it cannot read /tmp, so anything the CLI must open
    itself (charm files and resources referenced from a bundle, ``attach-resource``
    arguments, deploy overlays) has to live somewhere the snap can reach. This mirrors
    ``jubilant.Juju._temp_dir``.

    Returns:
        Path to a directory readable by the juju CLI.
    """
    juju_binary = shutil.which("juju") or ""
    if "/snap/" in juju_binary:
        target = Path.home() / "snap" / "juju" / "common" / "k8s-operator-tests"
    else:
        target = Path(tempfile.gettempdir()) / "k8s-operator-tests"
    target.mkdir(parents=True, exist_ok=True)
    return target


def stage(source: Path, subdir: str = "") -> Path:
    """Copy a file into :func:`render_dir` so the juju CLI can read it.

    Args:
        source: File to copy.
        subdir: Subdirectory of the render directory to copy into. Pass the test module
            name so concurrent modules (or concurrent tox envs sharing $HOME) can't
            overwrite each other's staged charms and resources.

    Returns:
        Path to the staged copy.
    """
    target_dir = render_dir() / subdir if subdir else render_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / Path(source).name
    if target.resolve() != Path(source).resolve():
        shutil.copy(source, target)
    return target


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, interval: str = "10s") -> Iterator[None]:
    """Temporarily speed up the model's update-status hook interval.

    Args:
        juju: Jubilant Juju instance.
        interval: Hook interval to apply inside the context.

    Yields:
        None.
    """
    previous = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": interval})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": previous})


def wait_active(
    juju: jubilant.Juju,
    *apps: str,
    timeout: float,
    delay: float = DEFAULT_DELAY,
    successes: int = DEFAULT_SUCCESSES,
    raise_on_error: bool = True,
) -> jubilant.Status:
    """Wait until the given applications are active and their unit agents are idle.

    This is the equivalent of python-libjuju's ``wait_for_idle(status="active")``, which
    waited on the workload status *and* the agent status.

    Args:
        juju: Jubilant Juju instance.
        apps: Applications to wait for; empty means every application in the model.
        timeout: Timeout in seconds.
        delay: Seconds between status polls.
        successes: Consecutive successful polls required before returning.
        raise_on_error: Whether to abort as soon as an app or unit enters "error". Set
            false for charms that error transiently while settling, matching libjuju's
            ``wait_for_idle(raise_on_error=False)``.

    Returns:
        The final Status object.
    """
    return juju.wait(
        lambda status: (
            jubilant.all_active(status, *apps) and jubilant.all_agents_idle(status, *apps)
        ),
        error=(lambda status: jubilant.any_error(status, *apps)) if raise_on_error else None,
        timeout=timeout,
        delay=delay,
        successes=successes,
    )


def wait_blocked(
    juju: jubilant.Juju,
    *apps: str,
    timeout: float,
    delay: float = DEFAULT_DELAY,
    successes: int = DEFAULT_SUCCESSES,
) -> jubilant.Status:
    """Wait until the given applications are blocked and their unit agents are idle.

    Args:
        juju: Jubilant Juju instance.
        apps: Applications to wait for; empty means every application in the model.
        timeout: Timeout in seconds.
        delay: Seconds between status polls.
        successes: Consecutive successful polls required before returning.

    Returns:
        The final Status object.
    """
    return juju.wait(
        lambda status: (
            jubilant.all_blocked(status, *apps) and jubilant.all_agents_idle(status, *apps)
        ),
        error=lambda status: jubilant.any_error(status, *apps),
        timeout=timeout,
        delay=delay,
        successes=successes,
    )


def wait_idle(
    juju: jubilant.Juju,
    *apps: str,
    timeout: float,
    delay: float = DEFAULT_DELAY,
    successes: int = DEFAULT_SUCCESSES,
) -> jubilant.Status:
    """Wait until unit agents are idle, without asserting anything about workload status.

    Equivalent to libjuju's ``wait_for_idle(raise_on_error=False)`` with no status filter.

    Args:
        juju: Jubilant Juju instance.
        apps: Applications to wait for; empty means every application in the model.
        timeout: Timeout in seconds.
        delay: Seconds between status polls.
        successes: Consecutive successful polls required before returning.

    Returns:
        The final Status object.
    """
    return juju.wait(
        lambda status: jubilant.all_agents_idle(status, *apps),
        timeout=timeout,
        delay=delay,
        successes=successes,
    )


def unit_names(juju: jubilant.Juju, app: str) -> List[str]:
    """Return the unit names of an application, ordered by unit number.

    Args:
        juju: Jubilant Juju instance.
        app: Application name.

    Returns:
        Unit names, for example ``["k8s/0", "k8s/1"]``.
    """
    return sorted(juju.status().get_units(app), key=lambda name: int(name.rsplit("/", 1)[1]))


def get_leader(juju: jubilant.Juju, app: str) -> str:
    """Return the name of the leader unit of an application.

    Args:
        juju: Jubilant Juju instance.
        app: Application name.

    Returns:
        Leader unit name, for example ``k8s/0``.

    Raises:
        ValueError: if no leader was found.
    """
    for name, unit in juju.status().get_units(app).items():
        if unit.leader:
            return name
    raise ValueError(f"No leader found for app '{app}'")


def unit_port(status: jubilant.Status, app: str, unit: str) -> int:
    """Return the first opened port of a unit.

    Args:
        status: A Juju status object.
        app: Application name.
        unit: Unit name.

    Returns:
        The port number.
    """
    ports = status.get_units(app)[unit].open_ports
    assert ports, f"Unit {unit} has no opened ports"
    return int(ports[0].split("/")[0])


def get_rsc(
    juju: jubilant.Juju,
    unit: str,
    resource: str,
    namespace: Optional[str] = None,
    labels: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Get a kubectl resource list, optionally filtered by namespace and labels.

    Args:
        juju: Jubilant Juju instance.
        unit: k8s unit to run kubectl on, for example ``k8s/0``.
        resource: Resource type, for example ``pods``, ``nodes`` or ``pod/my-pod``.
        namespace: Optional namespace filter.
        labels: Optional label filter.

    Returns:
        List of resource dicts.
    """
    namespaced = f"-n {namespace}" if namespace else ""
    labeled = " ".join(f"-l {k}={v}" for k, v in labels.items()) if labels else ""
    task = juju.exec(f"k8s kubectl get {resource} {labeled} {namespaced} -o json", unit=unit)
    log.info("Parsing %s list...", resource)
    resource_obj = json.loads(task.stdout)
    if "/" in resource:
        return [resource_obj]
    assert resource_obj["kind"] == "List", f"Should have found a list of {resource}"
    return resource_obj["items"]


def ready_nodes(juju: jubilant.Juju, unit: str, expected_count: int) -> None:
    """Assert that exactly *expected_count* Kubernetes nodes are ready.

    Args:
        juju: Jubilant Juju instance.
        unit: k8s unit to run kubectl on.
        expected_count: Number of nodes expected to be ready.
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
            readiness = {
                node["metadata"]["name"]: all(
                    condition["status"] == "False"
                    for condition in node["status"]["conditions"]
                    if condition["type"] != "Ready"
                )
                for node in nodes
            }
            log.info("Found %d/%d nodes...", len(readiness), expected_count)
            assert len(readiness) == expected_count, f"Expect {expected_count} nodes in the list"
            for node, is_ready in readiness.items():
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
    """Wait for pods to reach one of the given phases.

    Args:
        juju: Jubilant Juju instance.
        unit: k8s unit to run kubectl on.
        name: Pod name, or None for every pod in the namespace.
        phase: Acceptable phases, for example ``Running`` or ``Succeeded``.
        namespace: Pod namespace.
        retry_times: Number of attempts.
        retry_delay_s: Seconds between attempts.
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
                current, pod_name = pod["status"]["phase"], pod["metadata"]["name"]
                assert current in phase, f"Pod {pod_name} not yet in phase {phase}"


def get_pod_logs(juju: jubilant.Juju, unit: str, name: str, namespace: str = "default") -> str:
    """Retrieve the logs of a pod.

    Args:
        juju: Jubilant Juju instance.
        unit: k8s unit to run kubectl on.
        name: Pod name.
        namespace: Pod namespace.

    Returns:
        The pod logs.
    """
    return juju.exec(f"k8s kubectl logs --namespace {namespace} pod/{name}", unit=unit).stdout


def get_kubeconfig(juju: jubilant.Juju, dest_dir: Path) -> Path:
    """Retrieve a kubeconfig from the k8s leader and write it to *dest_dir*.

    Args:
        juju: Jubilant Juju instance.
        dest_dir: Directory in which to write the kubeconfig.

    Returns:
        Path to the kubeconfig file.
    """
    kubeconfig_path = dest_dir / "kubeconfig"
    if kubeconfig_path.exists() and kubeconfig_path.stat().st_size:
        return kubeconfig_path
    task = juju.run(get_leader(juju, "k8s"), "get-kubeconfig")
    kubeconfig_path.parent.mkdir(exist_ok=True, parents=True)
    kubeconfig_path.write_text(task.results["kubeconfig"])
    assert kubeconfig_path.stat().st_size, "kubeconfig file is 0 bytes"
    return kubeconfig_path


def get_unit_cidrs(juju: jubilant.Juju, app: str, unit_num: int) -> List[str]:
    """Find the network CIDRs reachable from a unit.

    Args:
        juju: Jubilant Juju instance.
        app: Application name.
        unit_num: Unit number.

    Returns:
        Sorted list of network CIDRs.
    """
    task = juju.exec("ip --json route show", unit=f"{app}/{unit_num}")
    local_cidrs = set()
    for route in json.loads(task.stdout):
        try:
            cidr = ipaddress.ip_network(route.get("dst"))
        except ValueError:
            continue
        if cidr.prefixlen < 32:
            local_cidrs.add(str(cidr))
    return sorted(local_cidrs)
