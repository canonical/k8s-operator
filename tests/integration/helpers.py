# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Additions to tools missing from the jubilant library."""

# pylint: disable=too-many-arguments,too-many-positional-arguments

import contextlib
import functools
import ipaddress
import json
import logging
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import jubilant
import yaml
from tenacity import (
    Retrying,
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_fixed,
)

log = logging.getLogger(__name__)
CHARMCRAFT_DIRS = {
    "k8s": Path("charms/worker/k8s"),
    "k8s-worker": Path("charms/worker"),
}

# Mapping of Ubuntu series codename to version. Replaces juju.utils.get_series_version,
# which (in juju<=3.6) does not know about "resolute" (Ubuntu 26.04).
_SERIES_TO_VERSION = {
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
    "oracular": "24.10",
    "plucky": "25.04",
    "questing": "25.10",
    "resolute": "26.04",
}


def series_to_version(series: str) -> str:
    """Return the Ubuntu version for a series codename (e.g. ``jammy`` -> ``22.04``).

    Args:
        series: Ubuntu series codename.

    Returns:
        The matching Ubuntu version string.

    Raises:
        ValueError: if the series is unknown.
    """
    try:
        return _SERIES_TO_VERSION[series]
    except KeyError as err:
        raise ValueError(f"Unknown series: {series}") from err


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, interval: str = "1m"):
    """Temporarily speed up the update-status hook interval for the model.

    Args:
        juju: jubilant Juju instance
        interval: update-status-hook-interval to set while in the context
    """
    old = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": interval})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": old})


def charm_ref_name(ref: str) -> str:
    """Extract the bare charm name from a bundle charm reference.

    Handles bare names (``k8s``), charmhub refs (``ch:amd64/k8s``,
    ``ch:amd64/jammy/k8s``), and local paths.

    Args:
        ref: A charm reference string.

    Returns:
        The charm name.
    """
    name = str(ref).rsplit("/", 1)[-1]
    if ":" in name:
        name = name.split(":")[-1]
    return name


def get_unit_cidrs(juju: jubilant.Juju, app_name: str, unit_num: int) -> List[str]:
    """Find unit network cidrs on a unit.

    Args:
        juju: jubilant Juju instance
        app_name: string name of application
        unit_num: integer number of a juju unit

    Returns:
        list of network cidrs
    """
    task = juju.exec("ip --json route show", unit=f"{app_name}/{unit_num}")
    routes = json.loads(task.stdout)
    local_cidrs = set()
    for rt in routes:
        try:
            cidr = ipaddress.ip_network(rt.get("dst"))
        except ValueError:
            continue
        if cidr.prefixlen < 32:
            local_cidrs.add(str(cidr))
    return sorted(local_cidrs)


def get_rsc(
    juju: jubilant.Juju, unit: str, resource, namespace=None, labels=None
) -> List[Dict[str, Any]]:
    """Get Resource list optionally filtered by namespace and labels.

    Args:
        juju: jubilant Juju instance
        unit: name of any k8s unit (e.g. "k8s/0")
        resource: string resource type (e.g. pods, services, nodes)
        namespace: string namespace
        labels: dict of labels to use for filtering

    Returns:
        list of resources
    """
    namespaced = f"-n {namespace}" if namespace else ""
    labeled = " ".join(f"-l {k}={v}" for k, v in labels.items()) if labels else ""
    cmd = f"k8s kubectl get {resource} {labeled} {namespaced} -o json"

    task = juju.exec(cmd, unit=unit)
    log.info("Parsing %s list...", resource)
    resource_obj = json.loads(task.stdout)
    if "/" in resource:
        return [resource_obj]
    assert resource_obj["kind"] == "List", f"Should have found a list of {resource}"
    return resource_obj["items"]


@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(15))
def ready_nodes(juju: jubilant.Juju, unit: str, expected_count: int):
    """Get a list of the ready nodes.

    Args:
        juju: jubilant Juju instance
        unit: k8s unit name (e.g. "k8s/0")
        expected_count: number of expected nodes
    """
    log.info("Finding all nodes...")
    nodes = get_rsc(juju, unit, "nodes")
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


def wait_pod_phase(
    juju: jubilant.Juju,
    unit: str,
    name: Optional[str],
    *phase: str,
    namespace: str = "default",
    retry_times: int = 30,
    retry_delay_s: int = 15,
):
    """Wait for the pod to reach the specified phase (e.g. Succeeded).

    Args:
        juju: jubilant Juju instance
        unit: k8s unit name (e.g. "k8s/0")
        name: the pod name or all pods if None
        phase: expected phase
        namespace: pod namespace
        retry_times: the number of retries
        retry_delay_s: retry interval

    """
    pod_resource = "pod" if name is None else f"pod/{name}"
    for attempt in Retrying(
        stop=stop_after_attempt(retry_times),
        wait=wait_fixed(retry_delay_s),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            for pod in get_rsc(juju, unit, pod_resource, namespace=namespace):
                _phase, _name = pod["status"]["phase"], pod["metadata"]["name"]
                assert _phase in phase, f"Pod {_name} not yet in phase {phase}"


def get_pod_logs(
    juju: jubilant.Juju,
    unit: str,
    name: str,
    namespace: str = "default",
) -> str:
    """Retrieve pod logs.

    Args:
        juju: jubilant Juju instance
        unit: k8s unit name (e.g. "k8s/0")
        name: pod name
        namespace: pod namespace

    Returns:
        the pod logs as string.
    """
    cmd = " ".join(["k8s kubectl logs", f"--namespace {namespace}", f"pod/{name}"])
    task = juju.exec(cmd, unit=unit)
    return task.stdout


def get_leader(juju: jubilant.Juju, app: str) -> str:
    """Find leader unit of an application.

    Args:
        juju: jubilant Juju instance
        app: application name

    Returns:
        str: name of the leader unit (e.g. "k8s/0")

    Raises:
        ValueError: No leader found
    """
    for name, unit in juju.status().get_units(app).items():
        if unit.leader:
            return name
    raise ValueError("No leader found")


def get_kubeconfig(juju: jubilant.Juju, tmp_path: Path, module_name: str) -> Path:
    """Retrieve kubeconfig from the k8s leader.

    Args:
        juju: jubilant Juju instance
        tmp_path: base temporary path to store the kubeconfig
        module_name: name of the test module

    Returns:
        path to the kubeconfig file
    """
    kubeconfig_path = tmp_path / module_name / "kubeconfig"
    if kubeconfig_path.exists() and kubeconfig_path.stat().st_size:
        return kubeconfig_path
    leader = get_leader(juju, "k8s")
    task = juju.run(leader, "get-kubeconfig")
    kubeconfig_path.parent.mkdir(exist_ok=True, parents=True)
    kubeconfig_path.write_text(task.results["kubeconfig"])
    assert Path(kubeconfig_path).stat().st_size, "kubeconfig file is 0 bytes"
    return kubeconfig_path


@dataclass
class Markings:
    """Test markings for the bundle.

    Attrs:
        series: Series for Machines in the bundle
        apps_local: List of application names needing local files to replace charm urls
        apps_channel: Mapping of application names to channels
        apps_resources: Mapping of application names to resources
    """

    series: Optional[str] = None
    apps_local: List[str] = field(default_factory=list)
    apps_channel: Mapping = field(default_factory=dict)
    apps_resources: Mapping = field(default_factory=dict)


def _narrow(potentials: Iterable[Path], prefix: str, arch: str, base: str) -> Path:
    """Narrow a set of candidate charm files to a single match.

    Args:
        potentials: candidate charm file paths
        prefix: charm filename prefix (e.g. "k8s_")
        arch: cloud architecture (e.g. "amd64")
        base: ubuntu version to match (e.g. "26.04")

    Returns:
        the single matching charm file

    Raises:
        FileNotFoundError: if zero or more than one charm file matches
    """
    by_arch_base = filter(lambda s: arch in str(s) and base in str(s), potentials)
    by_name = filter(lambda s: s.name.startswith(prefix), by_arch_base)
    exist = filter(lambda s: s.exists(), by_name)
    if options := set(exist):
        if len(options) > 1:
            log.warning(
                "Too many charm files found matching filters\n"
                "   starting-with: '%s'\n"
                "   with arch: '%s'\n"
                "   with base: '%s'\n"
                "options: %s",
                prefix,
                arch,
                base,
                ", ".join(map(str, options)),
            )
            raise FileNotFoundError("Too many charm files found")
        return options.pop()
    raise FileNotFoundError("No charm files found")


@dataclass
class Charm:
    """Represents source charms in this repository.

    Attrs:
        path:       Path to the charmcraft file
        arch:       Cloud Architecture
        metadata:   Charm's metadata
        name:       Name of the charm from the metadata
        local_path: Path to the built charm file
    """

    path: Path
    arch: str
    _charmfile: Optional[Path] = None

    @cached_property
    def metadata(self) -> dict:
        """Charm Metadata."""
        return yaml.safe_load((self.path / "charmcraft.yaml").read_text())

    @property
    def name(self) -> str:
        """Name defined by the charm."""
        return self.metadata["name"]

    @property
    def local_path(self) -> Path:
        """Local path to the charm.

        Returns:
            Path to the built charm file
        Raises:
            FileNotFoundError: the charm file wasn't found
        """
        if self._charmfile is None:
            raise FileNotFoundError(f"{self.name}_*.charm not found")
        return self._charmfile

    def charmhub_url(self, series: Optional[str] = None) -> str:
        """Return the charmhub reference for this charm pinned to the architecture.

        Args:
            series: optional series to include in the reference

        Returns:
            charmhub reference, e.g. "ch:amd64/jammy/k8s" or "ch:amd64/k8s"
        """
        if series:
            return f"ch:{self.arch}/{series}/{self.name}"
        return f"ch:{self.arch}/{self.name}"

    @classmethod
    def find(cls, ref: str, arch: str) -> Optional["Charm"]:
        """Find a charm managed in this repo based on its name.

        Args:
            ref: Charm reference or charm name
            arch: Cloud architecture

        Returns:
            Charm object or None
        """
        name = charm_ref_name(ref)
        if charmcraft := CHARMCRAFT_DIRS.get(name):
            return cls(charmcraft, arch)
        return None

    def resolve(self, charm_files: List[str], base: str) -> "Charm":
        """Build or find the charm.

        Args:
            charm_files: charm files passed via --charm-file
            base (str): Base release for the charm (e.g. "26.04")

        Return:
            self (Charm): the resolved charm

        Raises:
            FileNotFoundError: the charm file wasn't found
        """
        prefix = f"{self.name}_"
        if self._charmfile is None:
            try:
                charm_name = prefix + "*.charm"
                potentials = chain(
                    map(Path, charm_files),  # Look in pytest arguments
                    Path().glob(charm_name),  # Look in top-level path
                    self.path.glob(charm_name),  # Look in charm-level path
                )
                self._charmfile = _narrow(potentials, prefix, self.arch, base)
                log.info("For %s found charmfile %s", self.name, self._charmfile)
            except FileNotFoundError as err:
                log.warning(f"For {self.name} failed locating existing {err=}, build instead")

        if self._charmfile is None:
            log.info("For %s build charmfiles", self.name)
            subprocess.run(["charmcraft", "pack"], cwd=self.path, check=True)
            self._charmfile = _narrow(self.path.glob(prefix + "*.charm"), prefix, self.arch, base)
            log.info("For %s built charmfile %s", self.name, self._charmfile)

        if self._charmfile is None:
            raise FileNotFoundError(f"{prefix}*.charm not found")
        return self


@dataclass
class Bundle:
    """Represents a test bundle.

    Attrs:
        path:            Path to the bundle file
        arch:            Cloud Architecture
        series:          Series for Machines in the bundle
        content:         Loaded content from the path
        applications:    Mapping of applications in the bundle.
        needs_trust:     True if the bundle needs to be trusted
    """

    path: Path
    arch: str
    series: Optional[str] = None
    _content: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, request, juju: jubilant.Juju) -> Tuple["Bundle", Markings]:
        """Craft a bundle for the given test environment.

        Args:
            request: pytest request object
            juju: jubilant Juju instance

        Returns:
            Bundle object for the test
            Markings from the test
        """
        bundle_marker = request.node.get_closest_marker("bundle")
        assert bundle_marker, "No bundle marker found"
        kwargs = {**bundle_marker.kwargs}

        if val := kwargs.pop("file", None):
            path = Path(__file__).parent / "data" / val
        else:
            log.warning("No file specified, using default test-bundle.yaml")
            path = Path(__file__).parent / "data" / "test-bundle.yaml"

        arch = cloud_arch(juju)
        assert arch, "Architecture must be known before customizing the bundle"

        series = request.config.option.series

        bundle = cls(path=path, arch=arch, series=series)
        assert not all(_ in kwargs for _ in ("apps_local", "apps_channel")), (
            "Cannot use both apps_local and apps_channel"
        )

        return bundle, Markings(**kwargs)

    @property
    def content(self) -> Dict[str, Any]:
        """Yaml content of the bundle loaded into a dict.

        Returns:
            Dict: bundle content
        """
        if not self._content:
            loaded = yaml.safe_load(self.path.read_bytes())
            self.series = self.series or loaded.get("series")
            for app in loaded["applications"].values():
                name = charm_ref_name(app["charm"])
                app["charm"] = f"ch:{self.arch}/{name}"
            self._content = loaded
        return self._content

    @property
    def applications(self) -> Mapping[str, dict]:
        """Mapping of all available application in the bundle.

        Returns:
            Mapping: application name to application details
        """
        return self.content["applications"]

    @property
    def needs_trust(self) -> bool:
        """Check if the bundle needs to be trusted.

        Returns:
            bool: True if the bundle needs to be trusted
        """
        return any(app.get("trust", False) for app in self.applications.values())

    def discover_charm_files(self, charm_files: List[str]) -> Dict[str, Charm]:
        """Discover charm files for the applications in the bundle.

        Args:
            charm_files: charm files passed via --charm-file

        Returns:
            Mapping: application name to Charm object
        """
        app_to_charm = {}
        for app in self.applications.values():
            if charm := Charm.find(app["charm"], self.arch):
                charm.resolve(charm_files, series_to_version(self.series or "jammy"))
                app_to_charm[charm.name] = charm
        return app_to_charm

    def apply_marking(self, request, juju: jubilant.Juju, markings: Markings):
        """Customize the bundle for the test.

        Args:
            request: pytest request object
            juju: jubilant Juju instance
            markings: Markings from the test
        """
        _type, _vms = cloud_type(juju, request.config.getoption("--lxd-containers"))
        if _type == "lxd" and not _vms:
            log.info("Drop lxd machine constraints")
            self.drop_constraints()
        if _type == "lxd" and _vms:
            log.info("Constrain lxd machines with virt-type: virtual-machine")
            self.add_constraints({"virt-type": "virtual-machine"})

        charms = self.discover_charm_files(request.config.option.charm_files or [])

        empty_resource = {"snap-installation": request.config.option.snap_installation_resource}

        if series := request.config.option.series or markings.series:
            self.content["series"] = self.series = series

        for app in markings.apps_local:
            assert app in charms, f"App={app} doesn't have a local charm"
            rsc = markings.apps_resources.get(app) or empty_resource
            self.switch(app, charm=charms[app], channel=None, resources=rsc)

        for app, channel in markings.apps_channel.items():
            rsc = markings.apps_resources.get(app)
            self.switch(app, charm=charms[app], channel=channel, resources=rsc)

    def switch(
        self,
        name: str,
        charm: Charm,
        channel: Optional[str] = None,
        resources: Optional[dict] = None,
    ):
        """Replace charmhub application with a local path or specific channel.

        Args:
            name (str):    Which application
            charm (Charm): Which charm to use
            channel (Optional[str]): If specified use channel, otherwise use local path
            resources (dict): Optional resources to add

        Raises:
            FileNotFoundError: if the local charm file is not found
        """
        app = self.applications.get(name)
        if not app:
            return  # Skip if the application is not in the bundle
        if not channel and charm._charmfile is None:
            raise FileNotFoundError(f"Charm={charm.name} for App={app} not found")
        if channel:
            app["charm"] = charm.charmhub_url(self.series)
            app["channel"] = channel
        else:
            app["charm"] = str(charm.local_path.resolve())
            app["channel"] = None
        if resources:
            app["resources"] = resources

    def drop_constraints(self):
        """Remove constraints on applications. Useful for testing on lxd."""
        for app in self.applications.values():
            app["constraints"] = ""

    def add_constraints(self, constraints: Dict[str, str]):
        """Add constraints to applications.

        Args:
            constraints:  Mapping of constraints to add to applications.
        """
        for app in self.applications.values():
            if app.get("num_units", 0) < 1:
                log.info("Skipping constraints for subordinate charm: %s", app["charm"])
                continue
            val: str = app.get("constraints", "")
            existing = dict(kv.split("=", 1) for kv in val.split())
            existing.update(constraints)
            app["constraints"] = " ".join(f"{k}={v}" for k, v in existing.items())

    def render(self, tmp_path: Path) -> Path:
        """Path to written bundle config to be deployed.

        Args:
            tmp_path: temporary path to write the bundle

        Returns:
            Path to the written bundle
        """
        self.add_constraints({"arch": self.arch})
        target = tmp_path / "bundles" / self.path.name
        target.parent.mkdir(exist_ok=True, parents=True)
        yaml.safe_dump(self.content, target.open("w"))
        return target

    def is_deployed(self, juju: jubilant.Juju) -> bool:
        """Check if model has apps defined by the bundle.

        If all apps are deployed, wait for model to be idle

        Args:
            self:  Bundle object
            juju: jubilant Juju instance

        Returns:
            true if all apps and relations are in place and units are idle
        """
        bundle = yaml.safe_load(self.path.open())
        apps = bundle["applications"]
        status = juju.status()
        for app, conf in apps.items():
            if app not in status.apps:
                log.warning(
                    "Cannot use existing model(%s): Application (%s) isn't deployed",
                    juju.model,
                    app,
                )
                return False
            min_units = conf.get("num_units") or 1
            num_units = len(status.get_units(app))
            if num_units < min_units:
                log.warning(
                    "Cannot use existing model(%s): "
                    "Application(%s) has insufficient units %d < %d",
                    juju.model,
                    app,
                    num_units,
                    min_units,
                )
                return False
        juju.wait(jubilant.all_agents_idle, timeout=20 * 60)
        return True


@functools.lru_cache
def cloud_arch(juju: jubilant.Juju) -> str:
    """Return current architecture of the selected controller.

    Args:
        juju (jubilant.Juju): jubilant Juju instance

    Returns:
        string describing current architecture of the underlying cloud
    """
    ctrl_prefix = ""
    if juju.model and ":" in juju.model:
        ctrl_prefix = juju.model.split(":", 1)[0] + ":"
    controller_model = jubilant.Juju(model=f"{ctrl_prefix}controller")
    arch = set()
    for machine in controller_model.status().machines.values():
        # machine.hardware is a string like "arch=amd64 cores=2 mem=4096M"
        for kv in machine.hardware.split():
            key, _, value = kv.partition("=")
            if key == "arch":
                arch.add(value)
    return arch.pop().strip()


@functools.lru_cache
def cloud_type(juju: jubilant.Juju, lxd_containers: bool) -> Tuple[str, bool]:
    """Return current cloud type of the selected controller.

    Args:
        juju (jubilant.Juju): jubilant Juju instance
        lxd_containers (bool): value of the --lxd-containers option

    Returns:
        Tuple:
            string describing current type of the underlying cloud
            bool   describing if VMs are enabled
    """
    cloud_name = juju.status().model.cloud
    clouds = json.loads(juju.cli("clouds", "--all", "--format", "json", include_model=False))
    _type = clouds[cloud_name]["type"]
    vms = True  # Assume VMs are enabled
    if _type == "lxd":
        vms = not lxd_containers
    return _type, vms
