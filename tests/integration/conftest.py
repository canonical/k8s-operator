# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""
import asyncio
import contextlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

import juju.utils
import pytest
import pytest_asyncio
import yaml
from juju.application import Application
from juju.model import Model
from juju.tag import untag
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from pytest_operator.plugin import OpsTest

from .cos_substrate import LXDSubstrate
from .helpers import get_unit_cidrs, is_deployed

log = logging.getLogger(__name__)
TEST_DATA = Path(__file__).parent / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"
DEFAULT_RESOURCES = {"snap-installation": None}


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    --charm-file    can be used multiple times, specifies which local charm files are available
    --upgrade-from  instruct tests to start with a specific channel, and upgrade to these charms

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", dest="charm_files", action="append", default=[])
    parser.addoption(
        "--snap-installation-resource", default=str(DEFAULT_SNAP_INSTALLATION.resolve())
    )
    parser.addoption("--cos", action="store_true", default=False, help="Run COS integration tests")
    parser.addoption(
        "--apply-proxy", action="store_true", default=False, help="Apply proxy to model-config"
    )
    parser.addoption(
        "--lxd-containers",
        action="store_true",
        default=False,
        help="If cloud is LXD, use containers",
    )
    parser.addoption(
        "--upgrade-from", dest="upgrade_from", default=None, help="Charms channel to upgrade from"
    )


def pytest_configure(config):
    """Add pytest configuration args.

    Args:
        config: Pytest config.
    """
    config.addinivalue_line("markers", "cos: mark COS integration tests.")
    config.addinivalue_line("markers", "bundle_file(name): specify a YAML bundle file for a test.")


def pytest_collection_modifyitems(config, items):
    """Add cos marker parsing.

    Called after collection has been performed. May filter or re-order the items in-place.

    Args:
        config (pytest.Config): The pytest config object.
        items (List[pytest.Item]): List of item objects.
    """
    if not config.getoption("--cos"):
        skip_cos = pytest.mark.skip(reason="need --cos option to run")
        for item in items:
            if item.get_closest_marker("cos"):
                item.add_marker(skip_cos)


@dataclass
class Charm:
    """Represents source charms.

    Attrs:
        ops_test:  Instance of the pytest-operator plugin
        arch:      Cloud Architecture
        path:      Path to the charm file
        metadata:  Charm's metadata
        app_name:  Preferred name of the juju application
    """

    ops_test: OpsTest
    arch: str
    path: Path
    _charmfile: Optional[Path] = None
    _URL_RE = re.compile(r"ch:(?P<arch>\w+)/(?P<series>\w+)/(?P<charm>.+)")

    @staticmethod
    def craft_url(charm: str, series: str, arch: str) -> str:
        """Craft a charm URL.

        Args:
            charm:  Charm name
            series: Cloud series
            arch:   Cloud architecture

        Returns:
            string: URL to the charm
        """
        if m := Charm._URL_RE.match(charm):
            charm = m.group("charm")
        return f"ch:{arch}/{series}/{charm}"

    @property
    def metadata(self) -> dict:
        """Charm Metadata."""
        return yaml.safe_load((self.path / "charmcraft.yaml").read_text())

    @property
    def app_name(self) -> str:
        """Suggested charm name."""
        return self.metadata["name"]

    async def resolve(self, charm_files: List[str]) -> Path:
        """Build or find the charm with ops_test.

        Args:
            charm_files: The list charms files to resolve

        Return:
            path to charm file

        Raises:
            FileNotFoundError: the charm file wasn't found
        """
        if self._charmfile is None:
            try:
                header = f"{self.app_name}_"
                charm_name = header + "*.charm"
                potentials = chain(
                    map(Path, charm_files),  # Look in pytest arguments
                    Path().glob(charm_name),  # Look in top-level path
                    self.path.glob(charm_name),  # Look in charm-level path
                )
                arch_choices = filter(lambda s: self.arch in str(s), potentials)
                self._charmfile, *_ = filter(lambda s: s.name.startswith(header), arch_choices)
                log.info("For %s found charmfile %s", self.app_name, self._charmfile)
            except ValueError:
                log.warning("No pre-built charm is available, let's build it")
        if self._charmfile is None:
            log.info("For %s build charmfile", self.app_name)
            self._charmfile = await self.ops_test.build_charm(self.path)
        if self._charmfile is None:
            raise FileNotFoundError(f"{self.app_name}_*.charm not found")
        return self._charmfile.resolve()


@dataclass
class Bundle:
    """Represents test bundle.

    Attrs:
        ops_test:      Instance of the pytest-operator plugin
        path:          Path to the bundle file
        content:       Loaded content from the path
        arch:          Cloud Architecture
        render:        Path to a rendered bundle
        applications:  Mapping of applications in the bundle.
    """

    ops_test: OpsTest
    path: Path
    arch: str
    _content: Mapping = field(default_factory=dict)

    @classmethod
    async def create(cls, ops_test: OpsTest, path: Path) -> "Bundle":
        """Create a bundle object.

        Args:
            ops_test:  Instance of the pytest-operator plugin
            path:      Path to the bundle file

        Returns:
            Bundle:    Instance of the Bundle
        """
        arch = await cloud_arch(ops_test)
        _type, _vms = await cloud_type(ops_test)
        bundle = cls(ops_test, path, arch)
        if _type == "lxd" and not _vms:
            log.info("Drop lxd machine constraints")
            bundle.drop_constraints()
        if _type == "lxd" and _vms:
            log.info("Constrain lxd machines with virt-type: virtual-machine")
            bundle.add_constraints({"virt-type": "virtual-machine"})
        return bundle

    @property
    def content(self) -> Mapping:
        """Yaml content of the bundle loaded into a dict"""
        if not self._content:
            loaded = yaml.safe_load(self.path.read_bytes())
            series = loaded.get("series", "focal")
            for app in loaded["applications"].values():
                app["charm"] = Charm.craft_url(app["charm"], series=series, arch=self.arch)
            self._content = loaded
        return self._content

    @property
    def applications(self) -> Mapping[str, dict]:
        """Mapping of all available application in the bundle."""
        return self.content["applications"]

    @property
    def render(self) -> Path:
        """Path to written bundle config to be deployed."""
        self.add_constraints({"arch": self.arch})
        target = self.ops_test.tmp_path / "bundles" / self.path.name
        target.parent.mkdir(exist_ok=True, parents=True)
        yaml.safe_dump(self.content, target.open("w"))
        return target

    def switch(self, name: str, path: Optional[Path] = None, channel: Optional[str] = None):
        """Replace charmhub application with a local charm path or specific channel.

        Args:
            name (str):    Which application
            path (Path):   Optional path to local charm
            channel (str): Optional channel to use

        Raises:
            ValueError: if both path and channel are provided, or neither are provided
        """
        app = self.applications.get(name)
        if not app:
            return  # Skip if the application is not in the bundle
        if (not path and not channel) or (path and channel):
            raise ValueError("channel and path are mutually exclusive")
        if path:
            app["charm"] = str(path.resolve())
            app["channel"] = None
            app["resources"] = DEFAULT_RESOURCES
        if channel:
            app["charm"] = name
            app["channel"] = channel

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
    arch = set(
        machine.safe_data["hardware-characteristics"]["arch"]
        for machine in controller_model.machines.values()
    )
    return arch.pop()


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


async def cloud_proxied(ops_test: OpsTest):
    """Setup a cloud proxy settings if necessary

    Test if ghcr.io is reachable through a proxy, if so,
    Apply expected proxy config to juju model.

    Args:
        ops_test (OpsTest): ops_test plugin
    """
    assert ops_test.model, "Model must be present"
    controller = await ops_test.model.get_controller()
    controller_model = await controller.get_model("controller")
    proxy_config_file = TEST_DATA / "static-proxy-config.yaml"
    proxy_configs = yaml.safe_load(proxy_config_file.read_text())
    local_no_proxy = await get_unit_cidrs(controller_model, "controller", 0)
    no_proxy = {*proxy_configs["juju-no-proxy"], *local_no_proxy}
    proxy_configs["juju-no-proxy"] = ",".join(sorted(no_proxy))
    await ops_test.model.set_config(proxy_configs)


async def cloud_profile(ops_test: OpsTest):
    """Apply Cloud Specific Settings to the model

    Args:
        ops_test (OpsTest): ops_test plugin
    """
    _type, _vms = await cloud_type(ops_test)
    if _type == "lxd" and not _vms and ops_test.model:
        # lxd-profile to the model if the juju cloud is lxd.
        lxd = LXDSubstrate("", "")
        profile_name = f"juju-{ops_test.model.name}"
        lxd.remove_profile(profile_name)
        lxd.apply_profile("k8s.profile", profile_name)
    elif _type == "ec2" and ops_test.model:
        await ops_test.model.set_config({"container-networking-method": "local", "fan-config": ""})


@contextlib.asynccontextmanager
async def deploy_model(
    request: pytest.FixtureRequest,
    ops_test: OpsTest,
    model_name: str,
    bundle: Bundle,
):
    """Add a juju model, deploy apps into it, wait for them to be active.

    Args:
        request:           handle to pytest requests from calling fixture
        ops_test:          Instance of the pytest-operator plugin
        model_name:        name of the model in which to deploy
        bundle:            Bundle object to deploy or redeploy into the model

    Yields:
        model object
    """
    config: Optional[dict] = {}
    if request.config.option.model_config:
        config = ops_test.read_model_config(request.config.option.model_config)
    credential_name = ops_test.cloud_name
    if model_name not in ops_test.models:
        await ops_test.track_model(
            model_name,
            model_name=model_name,
            credential_name=credential_name,
            config=config,
        )
    with ops_test.model_context(model_name) as the_model:
        await cloud_profile(ops_test)
        async with ops_test.fast_forward("60s"):
            await the_model.deploy(bundle.render)
            await the_model.wait_for_idle(
                apps=list(bundle.applications),
                status="active",
                timeout=60 * 60,
            )
        try:
            yield the_model
        except GeneratorExit:
            log.fatal("Failed to determine model: model_name=%s", model_name)


def bundle_file(request) -> Path:
    """Helper to get bundle file.

    Args:
        request: pytest request object

    Returns:
        path to test's bundle file
    """
    _file = "test-bundle.yaml"
    bundle_marker = request.node.get_closest_marker("bundle_file")
    if bundle_marker:
        _file = bundle_marker.args[0]
    return Path(__file__).parent / "data" / _file


@pytest_asyncio.fixture(scope="module")
async def kubernetes_cluster(request: pytest.FixtureRequest, ops_test: OpsTest):
    """Deploy local kubernetes charms."""
    bundle_path = bundle_file(request)
    model = "main"

    with ops_test.model_context(model) as the_model:
        if await is_deployed(the_model, bundle_path):
            log.info("Using existing model.")
            yield ops_test.model
            return

    log.info("Deploying cluster using %s bundle.", bundle_path)

    bundle = await Bundle.create(ops_test, bundle_path)
    if request.config.option.apply_proxy:
        await cloud_proxied(ops_test)

    charms = [Charm(ops_test, bundle.arch, Path("charms") / p) for p in ("worker/k8s", "worker")]
    charm_files_args = request.config.option.charm_files
    DEFAULT_RESOURCES["snap-installation"] = request.config.option.snap_installation_resource
    charm_files = await asyncio.gather(*[charm.resolve(charm_files_args) for charm in charms])
    switch_to_path = {}
    for path, charm in zip(charm_files, charms):
        if upgrade_channel := request.config.option.upgrade_from:
            bundle.switch(charm.app_name, channel=upgrade_channel)
            switch_to_path[charm.app_name] = path
        else:
            bundle.switch(charm.app_name, path=path)

    async with deploy_model(request, ops_test, model, bundle) as the_model:
        await upgrade_model(the_model, switch_to_path)
        yield the_model


async def upgrade_model(model: Model, switch_to_path: dict[str, Path]):
    """Upgrade the model with the provided charms.

    Args:
        model:          Juju model
        switch_to_path: Mapping of app_name to charm

    """
    if not switch_to_path:
        return

    async def _refresh(app_name: str):
        """Refresh the application.

        Args:
            app_name: Name of the application to refresh
        """
        app: Application = model.applications[app_name]
        await app.refresh(path=switch_to_path[app_name], resources=DEFAULT_RESOURCES)

    await asyncio.gather(*[_refresh(app) for app in switch_to_path])
    await model.wait_for_idle(
        apps=list(switch_to_path.keys()),
        status="active",
        timeout=30 * 60,
    )


@pytest_asyncio.fixture(name="_grafana_agent", scope="module")
async def grafana_agent(kubernetes_cluster: Model):
    """Deploy Grafana Agent."""
    primary = kubernetes_cluster.applications["k8s"]
    data = primary.units[0].machine.safe_data
    machine_arch = data["hardware-characteristics"]["arch"]
    machine_series = juju.utils.get_version_series(data["base"].split("@")[1])

    await kubernetes_cluster.deploy(
        Charm.craft_url("grafana-agent", machine_series, machine_arch),
        channel="stable",
        series=machine_series,
    )
    await kubernetes_cluster.integrate("grafana-agent:cos-agent", "k8s:cos-agent")
    await kubernetes_cluster.integrate("grafana-agent:cos-agent", "k8s-worker:cos-agent")
    await kubernetes_cluster.integrate("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")

    yield

    await kubernetes_cluster.remove_application("grafana-agent")
    await kubernetes_cluster.applications["k8s"].destroy_relation(
        "cos-worker-tokens", "k8s-worker:cos-tokens", block_until_done=True
    )


@pytest_asyncio.fixture(scope="module")
async def cos_model(
    ops_test: OpsTest, kubernetes_cluster, _grafana_agent  # pylint: disable=W0613
):
    """Create a COS substrate and a K8s model."""
    container_name = "cos-substrate"
    network_name = "cos-network"
    manager = LXDSubstrate(container_name, network_name)

    config = manager.create_substrate()
    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    kubeconfig_path.write_text(config)
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))

    k8s_cloud = await ops_test.add_k8s(kubeconfig=config, skip_storage=False)
    k8s_model = await ops_test.track_model(
        "cos", cloud_name=k8s_cloud, keep=ops_test.ModelKeep.NEVER
    )
    yield k8s_model

    await ops_test.forget_model("cos", timeout=10 * 60, allow_failure=True)

    manager.teardown_substrate()


@pytest_asyncio.fixture(name="_cos_lite_installed", scope="module")
async def cos_lite_installed(ops_test: OpsTest, cos_model: Model):
    """Install COS Lite bundle."""
    log.info("Deploying COS bundle ...")
    cos_charms = ["alertmanager", "catalogue", "grafana", "loki", "prometheus", "traefik"]
    bundles = (
        ops_test.Bundle("cos-lite", "edge"),
        "tests/integration/data/cos-offers-overlay.yaml",
    )

    bundle, *overlays = await ops_test.async_render_bundles(*bundles)
    cmd = f"juju deploy -m {cos_model.name} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"COS Lite failed to deploy: {(stderr or stdout).strip()}"

    await cos_model.block_until(
        lambda: all(app in cos_model.applications for app in cos_charms),
        timeout=5 * 60,
    )
    await cos_model.wait_for_idle(status="active", timeout=20 * 60, raise_on_error=False)

    yield
    log.info("Removing COS Lite charms...")
    with ops_test.model_context("cos"):
        for charm in cos_charms:
            log.info("Removing %s...", charm)
            cmd = f"remove-application {charm} --destroy-storage --force --no-prompt"
            rc, stdout, stderr = await ops_test.juju(*shlex.split(cmd))
            log.info("%s", stdout or stderr)
            assert rc == 0
        await cos_model.block_until(
            lambda: all(app not in cos_model.applications for app in cos_charms), timeout=60 * 10
        )


@pytest_asyncio.fixture(scope="module")
async def traefik_url(cos_model: Model, _cos_lite_installed):
    """Fixture to fetch Traefik url."""
    action = await cos_model.applications["traefik"].units[0].run_action("show-proxied-endpoints")
    action = await action.wait()
    p_e = json.loads(action.results["proxied-endpoints"])

    yield p_e["traefik"]["url"]


@pytest_asyncio.fixture(scope="module")
async def expected_dashboard_titles():
    """Fixture to get expected Grafana dashboard titles."""
    grafana_dir = Path("charms/worker/k8s/src/grafana_dashboards")
    grafana_files = [p for p in grafana_dir.iterdir() if p.is_file() and p.name.endswith(".json")]
    titles = []
    for path in grafana_files:
        dashboard = json.loads(path.read_text())
        titles.append(dashboard["title"])
    return set(titles)


@pytest_asyncio.fixture(name="_related_grafana", scope="module")
async def related_grafana(ops_test: OpsTest, cos_model: Model, _cos_lite_installed):
    """Fixture to integrate with Grafana."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Integrating with Grafana")
        await model.integrate(
            "grafana-agent",
            f"{model_owner}/{cos_model_name}.grafana-dashboards",
        )
        with ops_test.model_context("cos") as k8s_model:
            await k8s_model.wait_for_idle(status="active")
        await model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Grafana SAAS ...")
        await model.remove_saas("grafana-dashboards")
    with ops_test.model_context("cos") as model:
        log.info("Removing Grafana Offer...")
        await model.remove_offer(f"{model.name}.grafana-dashboards", force=True)


@pytest_asyncio.fixture(scope="module")
async def grafana_password(cos_model, _related_grafana):
    """Fixture to get Grafana password."""
    action = await cos_model.applications["grafana"].units[0].run_action("get-admin-password")
    action = await action.wait()
    yield action.results["admin-password"]


@pytest_asyncio.fixture(scope="module")
async def related_prometheus(ops_test: OpsTest, cos_model, _cos_lite_installed):
    """Fixture to integrate with Prometheus."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Integrating with Prometheus")
        await model.integrate(
            "grafana-agent",
            f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
        )
        await model.wait_for_idle(status="active")
        with ops_test.model_context("cos") as model:
            await model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Prometheus Remote Write SAAS ...")
        await model.remove_saas("prometheus-receive-remote-write")

    with ops_test.model_context("cos") as model:
        log.info("Removing Prometheus Offer...")
        await model.remove_offer(f"{model.name}.prometheus-receive-remote-write", force=True)
