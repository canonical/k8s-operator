# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Additions to tools missing from juju library."""

# pylint: disable=too-many-arguments,too-many-positional-arguments

import contextlib
import ipaddress
import json
import logging
import shlex
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import jubilant
import jubilant.statustypes
import yaml
from literals import TEST_DATA
from pytest_jubilant import pack
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


PRECISE = "precise"
QUANTAL = "quantal"
RARING = "raring"
SAUCY = "saucy"
TRUSTY = "trusty"
UTOPIC = "utopic"
VIVID = "vivid"
WILY = "wily"
XENIAL = "xenial"
YAKKETY = "yakkety"
ZESTY = "zesty"
ARTFUL = "artful"
BIONIC = "bionic"
COSMIC = "cosmic"
DISCO = "disco"
EOAN = "eoan"
FOCAL = "focal"
GROOVY = "groovy"
HIRSUTE = "hirsute"
IMPISH = "impish"
JAMMY = "jammy"
KINETIC = "kinetic"
LUNAR = "lunar"
MANTIC = "mantic"
NOBLE = "noble"

UBUNTU_SERIES = {
    PRECISE: "12.04",
    QUANTAL: "12.10",
    RARING: "13.04",
    SAUCY: "13.10",
    TRUSTY: "14.04",
    UTOPIC: "14.10",
    VIVID: "15.04",
    WILY: "15.10",
    XENIAL: "16.04",
    YAKKETY: "16.10",
    ZESTY: "17.04",
    ARTFUL: "17.10",
    BIONIC: "18.04",
    COSMIC: "18.10",
    DISCO: "19.04",
    EOAN: "19.10",
    FOCAL: "20.04",
    GROOVY: "20.10",
    HIRSUTE: "21.04",
    IMPISH: "21.10",
    JAMMY: "22.04",
    KINETIC: "22.10",
    LUNAR: "23.04",
    MANTIC: "23.10",
    NOBLE: "24.04",
}

KUBERNETES = "kubernetes"
KUBERNETES_SERIES = {KUBERNETES: "kubernetes"}

ALL_SERIES_VERSIONS = {**UBUNTU_SERIES, **KUBERNETES_SERIES}


def get_series_version(series_name: str) -> str:
    """Get the OS version for a given series.

    Args:
        series_name (str): name of the series

    Returns:
        os version
    """
    if series_name not in ALL_SERIES_VERSIONS:
        raise NameError("Unknown series : %s", series_name)
    return ALL_SERIES_VERSIONS[series_name]


def get_version_series(version: str) -> str:
    """Get the series based on given OS version.

    Args:
        version (str): version of the OS

    Returns:
        series name
    """
    if version not in UBUNTU_SERIES.values():
        raise NameError("Unknown version : %s", version)
    return list(UBUNTU_SERIES.keys())[list(UBUNTU_SERIES.values()).index(version)]


def get_unit_cidrs(juju: jubilant.Juju, app_name: str, unit_num: int) -> List[str]:
    """Find unit network cidrs on a unit.

    Args:
        juju: jubilant juju object
        app_name: string name of application
        unit_num: integer number of a juju unit

    Returns:
        list of network cidrs
    """
    cmd = shlex.split("ip --json route show")
    result = juju.exec(*cmd, unit=f"{app_name}/{unit_num}")
    assert result.return_code == 0, "Failed to get routes"
    routes = json.loads(result.stdout)
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
    juju: jubilant.Juju, unit_name: str, resource: str, namespace=None, labels=None
) -> List[Dict[str, Any]]:
    """Get Resource list optionally filtered by namespace and labels.

    Args:
        juju: any juju object
        unit_name: the k8s unit name
        resource: string resource type (e.g. pods, services, nodes)
        namespace: string namespace
        labels: dict of labels to use for filtering

    Returns:
        list of resources
    """
    namespaced = f"-n {namespace}" if namespace else ""
    labeled = " ".join(f"-l {k}={v}" for k, v in labels.items()) if labels else ""
    cmd = shlex.split(f"k8s kubectl get {resource} {labeled} {namespaced} -o json")

    result = juju.exec(*cmd, unit=unit_name)  # Preload any missing binaries
    stdout, stderr = result.stdout, result.stderr
    assert result.return_code == 0, (
        f"\nFailed to get {resource} with kubectl\n\tstdout: '{stdout}'\n\tstderr: '{stderr}'"
    )
    log.info("Parsing %s list...", resource)
    resource_obj = json.loads(stdout)
    if "/" in resource:
        return [resource_obj]
    assert resource_obj["kind"] == "List", f"Should have found a list of {resource}"
    return resource_obj["items"]


@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(15))
def ready_nodes(juju: jubilant.Juju, unit_name: str, expected_count: int):
    """Get a list of the ready nodes.

    Args:
        juju: juju object
        unit_name: the k8s unit name
        expected_count: number of expected nodes
    """
    log.info("Finding all nodes...")
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    nodes = get_rsc(juju, unit_name, "nodes")
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
    unit_name: str,
    name: Optional[str],
    *phase: str,
    namespace: str = "default",
    retry_times: int = 30,
    retry_delay_s: int = 15,
):
    """Wait for the pod to reach the specified phase (e.g. Succeeded).

    Args:
        juju: jubilant object
        unit_name: the k8s unit name
        name: the pod name or all pods if None
        phase: expected phase
        namespace: pod namespace
        retry_times: the number of retries
        retry_delay_s: retry interval

    """
    pod_resource = "pod" if name is None else f"pod/{name}"
    # MIGRATION: switched from AsyncRetrying to sync Retrying (jubilant)
    for attempt in Retrying(
        stop=stop_after_attempt(retry_times),
        wait=wait_fixed(retry_delay_s),
        before_sleep=before_sleep_log(log, logging.WARNING),
    ):
        with attempt:
            for pod in get_rsc(juju, unit_name, pod_resource, namespace=namespace):
                _phase, _name = pod["status"]["phase"], pod["metadata"]["name"]
                assert _phase in phase, f"Pod {_name} not yet in phase {phase}"


def get_pod_logs(
    juju: jubilant.Juju,
    unit_name: str,
    name: str,
    namespace: str = "default",
) -> str:
    """Retrieve pod logs.

    Args:
        juju: jubilant object
        unit_name: the k8s unit name
        name: pod name
        namespace: pod namespace

    Returns:
        the pod logs as string.
    """
    cmd = " ".join(["k8s kubectl logs", f"--namespace {namespace}", f"pod/{name}"])
    result = juju.exec(*cmd, unit=unit_name)
    assert result.return_code == 0, f"Failed to retrieve pod {name} logs."
    return result.stdout


def get_leader(app: jubilant.statustypes.AppStatus) -> Tuple[str, jubilant.statustypes.UnitStatus]:
    """Find leader unit of an application.

    Args:
        app: Juju application

    Returns:
        Tuple[str, jubilant.statustypes.UnitStatus]:
        str: name to leader unit
        status: leader unit status

    Raises:
        ValueError: No leader found
    """
    for name, unit in app.units.items():
        if unit.leader:
            return name, unit
    raise ValueError("No leader found")


def get_kubeconfig(jubilant, module_name: str):
    """Retrieve kubeconfig from the k8s leader.

    Args:
        jubilant: pytest-jubilant plugin
        module_name: name of the test module

    Returns:
        path to the kubeconfig file
    """
    kubeconfig_path = jubilant.tmp_path / module_name / "kubeconfig"
    if kubeconfig_path.exists() and kubeconfig_path.stat().st_size:
        return kubeconfig_path
    k8s = jubilant.model.applications["k8s"]
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    leader_idx = get_leader(k8s)
    leader = k8s.units[leader_idx]
    action = leader.run_action("get-kubeconfig")
    result = action.wait()
    completed = result.status == "completed" or result.results["return-code"] == 0
    assert completed, f"Failed to get kubeconfig {result=}"
    kubeconfig_path.parent.mkdir(exist_ok=True, parents=True)
    kubeconfig_path.write_text(result.results["kubeconfig"])
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


@dataclass
class Charm:
    """Represents source charms in this repository.

    Attrs:
        path:       Path to the charmcraft file
        url:        Charm URL
        metadata:   Charm's metadata
        name:       Name of the charm from the metadata
        local_path: Path to the built charm file
    """

    path: Path
    url: str
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

    @classmethod
    def find(cls, url: str) -> Optional["Charm"]:
        """Find a charm managed in this repo based on its name.

        Args:
            url: Charm url or charm name

        Returns:
            Charm object or None
        """
        if charmcraft := CHARMCRAFT_DIRS.get(url):
            return cls(charmcraft, url)
        return None

    def resolve(self, request, arch: str, base: str) -> "Charm":
        """Build or find the charm with jubilant.

        Args:
            request: pytest request object
            arch (str): Cloud architecture
            base (str): Base release for the charm

        Return:
            self (Charm): the resolved charm

        Raises:
            FileNotFoundError: the charm file wasn't found
        """

        def _narrow(potentials: Iterable[Path]) -> Path:
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

        prefix = f"{self.name}_"
        if self._charmfile is None:
            charm_files = request.config.option.charm_files or []
            try:
                charm_name = prefix + "*.charm"
                potentials = chain(
                    map(Path, charm_files),  # Look in pytest arguments
                    Path().glob(charm_name),  # Look in top-level path
                    self.path.glob(charm_name),  # Look in charm-level path
                )
                self._charmfile = _narrow(potentials)
                log.info("For %s found charmfile %s", self.name, self._charmfile)
            except FileNotFoundError as err:
                log.warning(f"For {self.name} failed locating existing {err=}, build instead")

        if self._charmfile is None:
            log.info("For %s build charmfiles", self.name)
            potentials = pack(self.path, platform=f"ubuntu@{base}:{arch}")
            self._charmfile = _narrow([potentials])
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
    def create(cls, juju, request) -> Tuple["Bundle", Markings]:
        """Craft a bundle for the given jubilant environment.

        Args:
            juju: Instance of the jubilant.Juju
            request: pytest request object

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
            # for app in loaded["applications"].values():
            #     url = URL.parse(app["charm"])
            #     url.architecture = self.arch
            #     app["charm"] = url
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

    def discover_charm_files(self, juju: jubilant.Juju, request) -> Dict[str, Charm]:
        """Discover charm files for the applications in the bundle.

        Args:
            juju: Instance of the pytest-jubilant plugin
            request:  pytest request object

        Returns:
            Mapping: application name to Charm object
        """
        app_to_charm = {}
        for app in self.applications.values():
            if charm := Charm.find(app["charm"]):
                charm.resolve(
                    juju,
                    request,
                    self.arch,
                    get_series_version(self.series or "jammy"),
                )
                app_to_charm[charm.name] = charm
        return app_to_charm

    def apply_marking(self, juju: jubilant.Juju, request, markings: Markings):
        """Customize the bundle for the test.

        Args:
            juju: Instance of the pytest-jubilant plugin
            request:  pytest request object
            markings: Markings from the test
        """
        # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
        _type, _vms = cloud_type(juju, request)
        if _type == "lxd" and not _vms:
            log.info("Drop lxd machine constraints")
            self.drop_constraints()
        if _type == "lxd" and _vms:
            log.info("Constrain lxd machines with virt-type: virtual-machine")
            self.add_constraints({"virt-type": "virtual-machine"})

        charms = self.discover_charm_files(juju, request)

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
        if not charm.local_path and not channel:
            raise FileNotFoundError(f"Charm={charm.name} for App={app} not found")
        if channel:
            app["charm"] = charm.url.with_series(self.series)
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
        yaml.dump(self.content, target.open("w"))
        return target

    def is_deployed(self, juju: jubilant.Juju) -> bool:
        """Check if model has apps defined by the bundle.

        If all apps are deployed, wait for model to be idle

        Args:
            self:  Bundle object
            juju:  juju from jubilant

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
            num_units = len(status.apps[app].units)
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


@lru_cache
def cloud_arch(juju: jubilant.Juju) -> str:
    """Return current architecture of the selected controller.

    Args:
        juju: jubilant.juju plugin

    Returns:
        string describing current architecture of the underlying cloud
    """
    assert juju.model, "Model must be present"
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    controller = jubilant.Juju(model="controller")
    status = controller.status()
    hardware_status = [m.hardware for m in status.machines.values()]
    arch: Set[str] = set()
    for hardware in hardware_status:
        arches = filter(lambda s: "arch" in s, hardware.split())
        arch.update(arch.split("=")[1].strip() for arch in arches)
    return arch.pop().strip()


@lru_cache
def cloud_type(juju: jubilant.Juju, request) -> Tuple[str, bool]:
    """Return current cloud type of the selected controller.

    Args:
        juju: jubilant plugin
        request: pytest request object

    Returns:
        Tuple:
            string describing current type of the underlying cloud
            bool   describing if VMs are enabled
    """
    assert juju.model, "Model must be present"
    controller = jubilant.Juju(model="controller")
    status = controller.status()
    clouds = json.loads(controller.cli("clouds", "--format=json", include_model=False))
    _type = clouds[status.model.cloud]["type"]

    vms = True  # Assume VMs are enabled
    if _type == "lxd":
        vms = not request.config.getoption("--lxd-containers")
    return _type, vms


@lru_cache
def cloud_proxied(juju: jubilant.Juju):
    """Set up a cloud proxy settings if necessary.

    If ghcr.io is reachable through a proxy apply expected proxy config to juju model.

    Args:
        juju: jubilant.juju plugin
    """
    proxy_config_file = TEST_DATA / "static-proxy-config.yaml"
    proxy_configs = yaml.safe_load(proxy_config_file.read_text())
    controller = jubilant.Juju(model="controller")
    local_no_proxy = get_unit_cidrs(controller, "controller", 0)
    no_proxy = {*proxy_configs["juju-no-proxy"], *local_no_proxy}
    proxy_configs["juju-no-proxy"] = ",".join(sorted(no_proxy))
    juju.model_config(proxy_configs)


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, duration: str):
    """Context manager that temporarily speeds up update-status hooks to fire every 10s."""
    old = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": duration})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": old})


def untag(prefix: str, s: str) -> str:
    if s and s.startswith(prefix):
        return s[len(prefix) :]
    return s


# def url_representer(dumper: yaml.Dumper, data: URL) -> yaml.ScalarNode:
#     """Yaml representer for the Charm URL object.

#     Args:
#         dumper: yaml dumper
#         data: URL object

#     Returns:
#         yaml.ScalarNode: yaml node
#     """
#     return dumper.represent_scalar("tag:yaml.org,2002:str", str(data))


# yaml.add_representer(URL, url_representer)
