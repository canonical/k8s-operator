# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Additions to tools missing from juju library."""

# pylint: disable=too-many-arguments,too-many-positional-arguments

import asyncio
import ipaddress
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from functools import cache, cached_property
from itertools import chain
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

import yaml
from juju import unit
from juju.model import Model
from pytest_operator.plugin import OpsTest
from tenacity import AsyncRetrying, before_sleep_log, retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)
CHARMCRAFT_DIRS = {"k8s": Path("charms/worker/k8s"), "k8s-worker": Path("charms/worker")}


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


async def get_leader(app) -> int:
    """Find leader unit of an application.

    Args:
        app: Juju application

    Returns:
        int: index to leader unit

    Raises:
        ValueError: No leader found
    """
    is_leader = await asyncio.gather(*(u.is_leader_from_status() for u in app.units))
    for idx, flag in enumerate(is_leader):
        if flag:
            return idx
    raise ValueError("No leader found")


@dataclass
class Markings:
    """Test markings for the bundle.

    Attrs:
        apps_local: List of application names needing local files to replace charm urls
        apps_channel: Mapping of application names to channels
        apps_resources: Mapping of application names to resources
    """

    apps_local: List[str] = field(default_factory=list)
    apps_channel: Mapping = field(default_factory=dict)
    apps_resources: Mapping = field(default_factory=dict)


@dataclass
class CharmUrl:
    """Represents a charm URL.

    Attrs:
        name:       Name of the charm in the store
        series:     Cloud series
        arch:       Cloud architecture
    """

    name: str
    series: str
    arch: str
    _URL_RE = re.compile(r"ch:(?P<arch>\w+)/(?P<series>\w+)/(?P<charm>.+)")

    @classmethod
    def craft(cls, name: str, series: str, arch: str) -> "CharmUrl":
        """Parse a charm URL.

        Args:
            name:   Name or URL of the charm
            series: Cloud series
            arch:   Cloud architecture

        Returns:
            CharmUrl object
        """
        if m := cls._URL_RE.match(name):
            name = m.group("charm")
        return cls(name, series, arch)

    @staticmethod
    def representer(dumper: yaml.Dumper, data: "CharmUrl") -> yaml.ScalarNode:
        """Yaml representer for the CharmUrl object.

        Args:
            dumper: yaml dumper
            data: CharmUrl object

        Returns:
            yaml.ScalarNode: yaml node
        """
        as_str = f"ch:{data.arch}/{data.series}/{data.name}"
        return dumper.represent_scalar("tag:yaml.org,2002:str", as_str)


@dataclass
class Charm:
    """Represents source charms in this repository.

    Attrs:
        path:       Path to the charmcraft file
        metadata:   Charm's metadata
        name:       Name of the charm from the metadata
        local_path: Path to the built charm file
    """

    path: Path
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
    @cache
    def find(cls, name: str) -> Optional["Charm"]:
        """Find a charm by name.

        Args:
            name: Name of the charm

        Returns:
            Charm object or None
        """
        if charmcraft := CHARMCRAFT_DIRS.get(name):
            return cls(charmcraft)
        return None

    async def resolve(self, ops_test: OpsTest, arch: str) -> "Charm":
        """Build or find the charm with ops_test.

        Args:
            ops_test:   Instance of the pytest-operator plugin
            arch (str): Cloud architecture

        Return:
            self (Charm): the resolved charm

        Raises:
            FileNotFoundError: the charm file wasn't found
        """
        prefix = f"{self.name}_"
        if self._charmfile is None:
            charm_files = ops_test.request.config.option.charm_files or []
            try:
                charm_name = prefix + "*.charm"
                potentials = chain(
                    map(Path, charm_files),  # Look in pytest arguments
                    Path().glob(charm_name),  # Look in top-level path
                    self.path.glob(charm_name),  # Look in charm-level path
                )
                arch_choices = filter(lambda s: arch in str(s), potentials)
                self._charmfile, *_ = filter(lambda s: s.name.startswith(prefix), arch_choices)
                log.info("For %s found charmfile %s", self.name, self._charmfile)
            except ValueError:
                log.warning("No pre-built charm is available, let's build it")
        if self._charmfile is None:
            log.info("For %s build charmfile", self.name)
            self._charmfile = await ops_test.build_charm(self.path)
        if self._charmfile is None:
            raise FileNotFoundError(f"{prefix}*.charm not found")
        return self


@dataclass
class Bundle:
    """Represents a test bundle.

    Attrs:
        path:            Path to the bundle file
        arch:            Cloud Architecture
        content:         Loaded content from the path
        applications:    Mapping of applications in the bundle.
    """

    path: Path
    arch: str
    _content: Mapping = field(default_factory=dict)

    @classmethod
    async def create(cls, ops_test) -> Tuple["Bundle", Markings]:
        """Craft a bundle for the given ops_test environment.

        Args:
            ops_test: Instance of the pytest-operator plugin

        Returns:
            Bundle object for the test
            Markings from the test
        """
        bundle_marker = ops_test.request.node.get_closest_marker("bundle")
        assert bundle_marker, "No bundle marker found"
        kwargs = {**bundle_marker.kwargs}

        if val := kwargs.pop("file", None):
            path = Path(__file__).parent / "data" / val
        else:
            log.warning("No file specified, using default test-bundle.yaml")
            path = Path(__file__).parent / "data" / "test-bundle.yaml"

        arch = await cloud_arch(ops_test)
        assert arch, "Architecture must be known before customizing the bundle"

        bundle = cls(path=path, arch=arch)
        assert not all(
            _ in kwargs for _ in ("apps_local", "apps_channel")
        ), "Cannot use both apps_local and apps_channel"

        return bundle, Markings(**kwargs)

    @property
    def content(self) -> Mapping:
        """Yaml content of the bundle loaded into a dict

        Returns:
            Mapping: bundle content
        """
        if not self._content:
            loaded = yaml.safe_load(self.path.read_bytes())
            series = loaded.get("series", "focal")
            for app in loaded["applications"].values():
                app["charm"] = CharmUrl.craft(app["charm"], series=series, arch=self.arch)
            self._content = loaded
        return self._content

    @property
    def applications(self) -> Mapping[str, dict]:
        """Mapping of all available application in the bundle.

        Returns:
            Mapping: application name to application details
        """
        return self.content["applications"]

    async def discover_charm_files(self, ops_test: OpsTest) -> Dict[str, Charm]:
        """Discover charm files for the applications in the bundle.

        Args:
            ops_test: Instance of the pytest-operator plugin

        Returns:
            Mapping: application name to Charm object
        """
        app_to_charm = {}
        for app in self.applications.values():
            if charm := Charm.find(app["charm"].name):
                await charm.resolve(ops_test, self.arch)
                app_to_charm[charm.name] = charm
        return app_to_charm

    async def apply_marking(self, ops_test: OpsTest, markings: Markings):
        """Customize the bundle for the test.

        Args:
            ops_test: Instance of the pytest-operator plugin
            markings: Markings from the test
        """
        _type, _vms = await cloud_type(ops_test)
        if _type == "lxd" and not _vms:
            log.info("Drop lxd machine constraints")
            self.drop_constraints()
        if _type == "lxd" and _vms:
            log.info("Constrain lxd machines with virt-type: virtual-machine")
            self.add_constraints({"virt-type": "virtual-machine"})

        charms = await self.discover_charm_files(ops_test)

        empty_resource = {
            "snap-installation": ops_test.request.config.option.snap_installation_resource
        }
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
            app["charm"] = charm.name
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


async def cloud_arch(ops_test: OpsTest) -> str:
    """Return current architecture of the selected controller

    Args:
        ops_test (OpsTest): ops_test plugin

    Returns:
        string describing current architecture of the underlying cloud
    """
    assert ops_test.model, "Model must be present"
    controller = await ops_test.model.get_controller()
    controller_model = await controller.get_model("controller")
    arch: Set[str] = {
        machine.safe_data["hardware-characteristics"]["arch"]
        for machine in controller_model.machines.values()
    }
    return arch.pop().strip()


async def cloud_type(ops_test: OpsTest) -> Tuple[str, bool]:
    """Return current cloud type of the selected controller

    Args:
        ops_test (OpsTest): ops_test plugin

    Returns:
        Tuple:
            string describing current type of the underlying cloud
            bool   describing if VMs are enabled
    """
    assert ops_test.model, "Model must be present"
    controller = await ops_test.model.get_controller()
    cloud = await controller.cloud()
    _type = cloud.cloud.type_
    vms = True  # Assume VMs are enabled
    if _type == "lxd":
        vms = not ops_test.request.config.getoption("--lxd-containers")
    return _type, vms


yaml.add_representer(CharmUrl, CharmUrl.representer)
