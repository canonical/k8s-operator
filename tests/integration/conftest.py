# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""
import asyncio
import contextlib
import json
import logging
import shlex
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import List, Mapping, Optional

import pytest
import pytest_asyncio
import yaml
from juju.model import Model
from juju.tag import untag
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from pytest_operator.plugin import OpsTest

from .cos_substrate import LXDSubstrate
from .helpers import get_address

log = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    --charm-file can be called multiple times for each
      supplied charm

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", dest="charm_files", action="append", default=[])
    parser.addoption("--cos", action="store_true", default=False, help="Run COS integration tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "cos: mark COS integration tests.")
    config.addinivalue_line("markers", "bundle_file(name): specify a YAML bundle file for a test.")
    config.addinivalue_line(
        "markers", "ignore_blocked: specify if the bundle deploy should ignore BlockedStatus."
    )


def pytest_collection_modifyitems(config, items):
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
        path:      Path to the charm file
        metadata:  Charm's metadata
        app_name:  Preferred name of the juju application
    """

    ops_test: OpsTest
    path: Path
    _charmfile: Optional[Path] = None

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
                self._charmfile, *_ = filter(lambda s: s.name.startswith(header), potentials)
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
        render:        Path to a rendered bundle
        applications:  Mapping of applications in the bundle.
    """

    ops_test: OpsTest
    path: Path
    _content: Mapping = field(default_factory=dict)

    @property
    def content(self) -> Mapping:
        """Yaml content of the bundle loaded into a dict"""
        if not self._content:
            self._content = yaml.safe_load(self.path.read_bytes())
        return self._content

    @property
    def applications(self) -> Mapping[str, dict]:
        """Mapping of all available application in the bundle."""
        return self.content["applications"]

    @property
    def render(self) -> Path:
        """Path to written bundle config to be deployed."""
        target = self.ops_test.tmp_path / "bundles" / self.path.name
        target.parent.mkdir(exist_ok=True, parents=True)
        yaml.safe_dump(self.content, target.open("w"))
        return target

    def switch(self, name: str, path: Path):
        """Replace charmhub application with a local charm path.

        Args:
            name (str):  Which application
            path (Path): Path to local charm
        """
        app = self.applications[name]
        app["charm"] = str(path.resolve())
        app["channel"] = None


@contextlib.asynccontextmanager
async def deploy_model(
    request: pytest.FixtureRequest,
    ops_test: OpsTest,
    model_name: str,
    bundle: Bundle,
    raise_on_blocked=True,
):
    """Add a juju model, deploy apps into it, wait for them to be active.

    Args:
        request:     handle to pytest requests from calling fixture
        ops_test:    Instance of the pytest-operator plugin
        model_name:  name of the model in which to deploy
        bundle:      Bundle object to deploy or redeploy into the model

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
        async with ops_test.fast_forward():
            await the_model.deploy(bundle.render)
            await the_model.wait_for_idle(
                apps=list(bundle.applications),
                status="active",
                raise_on_blocked=raise_on_blocked,
                timeout=15 * 60,
            )
        yield the_model


@pytest_asyncio.fixture(scope="module")
async def kubernetes_cluster(request: pytest.FixtureRequest, ops_test: OpsTest):
    """Deploy local kubernetes charms."""
    bundle_file = "test-bundle.yaml"
    bundle_marker = request.node.get_closest_marker("bundle_file")
    if bundle_marker:
        bundle_file = bundle_marker.args[0]

    raise_on_blocked = True
    ignore_blocked = request.node.get_closest_marker("ignore_blocked")
    if ignore_blocked:
        raise_on_blocked = False

    log.info(f"Deploying cluster using {bundle_file} bundle.")

    model = "main"
    charm_path = ("worker/k8s", "worker")
    charms = [Charm(ops_test, Path("charms") / p) for p in charm_path]
    charm_files = await asyncio.gather(
        *[charm.resolve(request.config.option.charm_files) for charm in charms]
    )
    bundle = Bundle(ops_test, Path(__file__).parent / "data" / bundle_file)
    for path, charm in zip(charm_files, charms):
        bundle.switch(charm.app_name, path)
    async with deploy_model(request, ops_test, model, bundle, raise_on_blocked) as the_model:
        yield the_model


@pytest_asyncio.fixture(scope="module")
async def grafana_agent(ops_test: OpsTest, kubernetes_cluster: Model):
    """Deploy Grafana Agent."""
    await kubernetes_cluster.deploy("grafana-agent", channel="stable")
    await kubernetes_cluster.integrate("grafana-agent:cos-agent", "k8s:cos-agent")
    await kubernetes_cluster.integrate("grafana-agent:cos-agent", "k8s-worker:cos-agent")
    await kubernetes_cluster.integrate("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")

    yield

    await kubernetes_cluster.remove_application("grafana-agent")


@pytest_asyncio.fixture(scope="module")
async def cos_model(ops_test: OpsTest, kubernetes_cluster: Model, grafana_agent):
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


@pytest_asyncio.fixture(scope="module")
async def cos_lite_installed(ops_test: OpsTest, cos_model: Model):
    """Install COS Lite bundle."""
    log.info("Deploying COS bundle ...")
    cos_charms = ["alertmanager", "catalogue", "grafana", "loki", "prometheus", "traefik"]
    overlays = [
        ops_test.Bundle("cos-lite", "edge"),
        Path("tests/integration/data/cos-offers-overlay.yaml"),
    ]

    bundle, *overlays = await ops_test.async_render_bundles(*overlays)
    cmd = f"juju deploy -m {cos_model.name} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"COS Lite failed to deploy: {(stderr or stdout).strip()}"

    await cos_model.block_until(
        lambda: all(app in cos_model.applications for app in cos_charms),
        timeout=60,
    )
    await cos_model.wait_for_idle(status="active", timeout=20 * 60, raise_on_error=False)

    yield
    log.info("Removing COS Lite charms...")
    with ops_test.model_context("cos"):
        for charm in cos_charms:
            log.info(f"Removing {charm}...")
            cmd = f"remove-application {charm} --destroy-storage --force --no-prompt"
            rc, stdout, stderr = await ops_test.juju(*shlex.split(cmd))
            log.info(f"{(stdout or stderr)})")
            assert rc == 0
            await cos_model.block_until(
                lambda: charm not in cos_model.applications, timeout=60 * 10
            )


@pytest_asyncio.fixture(scope="module")
async def traefik_address(ops_test: OpsTest, cos_model: Model, cos_lite_installed):
    """Fixture to get Traefik address."""
    with ops_test.model_context("cos"):
        address = await get_address(ops_test=ops_test, app_name="traefik")
    yield address


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


@pytest_asyncio.fixture(scope="module")
async def related_grafana(ops_test: OpsTest, cos_model: Model, cos_lite_installed):
    """Fixture to integrate with Grafana."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Integrating with Grafana")
        await ops_test.model.integrate(
            "grafana-agent",
            f"{model_owner}/{cos_model_name}.grafana-dashboards",
        )
        with ops_test.model_context("cos") as k8s_model:
            await k8s_model.wait_for_idle(status="active")
        await ops_test.model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Grafana SAAS ...")
        await ops_test.model.remove_saas("grafana-dashboards")
    with ops_test.model_context("cos") as model:
        log.info("Removing Grafana Offer...")
        await model.remove_offer(f"{model.name}.grafana-dashboards", force=True)


@pytest_asyncio.fixture(scope="module")
async def grafana_password(ops_test, cos_model: Model, related_grafana):
    """Fixture to get Grafana password."""
    with ops_test.model_context("cos"):
        action = (
            await ops_test.model.applications["grafana"].units[0].run_action("get-admin-password")
        )
        action = await action.wait()
        yield action.results["admin-password"]


@pytest_asyncio.fixture(scope="module")
async def related_prometheus(ops_test: OpsTest, cos_model, cos_lite_installed):
    """Fixture to integrate with Prometheus."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with ops_test.model_context("main") as model:
        log.info("Integrating with Prometheus")
        relation = await ops_test.model.integrate(
            "grafana-agent",
            f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
        )
        await ops_test.model.wait_for_idle(status="active")
        with ops_test.model_context("cos") as model:
            await model.wait_for_idle(status="active")

    yield

    with ops_test.model_context("main") as model:
        log.info("Removing Prometheus Remote Write SAAS ...")
        await ops_test.model.remove_saas("prometheus-receive-remote-write")

    with ops_test.model_context("cos") as model:
        log.info("Removing Prometheus Offer...")
        await model.remove_offer(f"{model.name}.prometheus-receive-remote-write", force=True)
