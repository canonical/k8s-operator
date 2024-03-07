# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""
import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from itertools import chain
from pathlib import Path
from typing import List, Mapping, Optional
import juju.utils
import shlex
import pytest
import pytest_asyncio
import yaml
import juju.model
from juju.tag import untag
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    --charm-file can be called multiple times for each
      supplied charm

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", dest="charm_files", action="append", default=[])


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
        async with ops_test.fast_forward("60s"):
            await the_model.deploy(bundle.render)
            await the_model.wait_for_idle(
                apps=list(bundle.applications),
                status="active",
                raise_on_blocked=True,
                timeout=15 * 60,
            )
        yield the_model


@pytest_asyncio.fixture(scope="module")
async def kubernetes_cluster(request: pytest.FixtureRequest, ops_test: OpsTest):
    """Deploy local kubernetes charms."""
    cluster_model = "main"
    charm_path = ("worker/k8s", "worker")
    charms = [Charm(ops_test, Path("charms") / p) for p in charm_path]
    charm_files = await asyncio.gather(
        *[charm.resolve(request.config.option.charm_files) for charm in charms]
    )

    bundle = Bundle(ops_test, Path(__file__).parent / "test-bundle.yaml")
    for path, charm in zip(charm_files, charms):
        bundle.switch(charm.app_name, path)

    async with deploy_model(request, ops_test, cluster_model, bundle) as the_model:
        yield the_model


@pytest_asyncio.fixture(scope="module")
async def cluster_kubeconfig(ops_test: OpsTest, kubernetes_cluster: juju.model.Model):
    """
    Fixture to pull the kubeconfig out of the kubernetes cluster
    """
    k8s = kubernetes_cluster.applications["k8s"].units[0]
    action = await k8s.run("k8s config")
    result = await action.wait()
    assert result.results["return-code"] == 0, "Failed to get kubeconfig with kubectl"

    kubeconfig_path = ops_test.tmp_path / "kubeconfig"
    kubeconfig_path.write_text(result.results["stdout"])
    yield kubeconfig_path


@pytest_asyncio.fixture(scope="module")
async def coredns_model(ops_test: OpsTest, cluster_kubeconfig: Path):
    """
    This fixture deploys Coredns on the specified Kubernetes (k8s) model for testing purposes.
    """
    log.info(f"Deploying Coredns ")

    coredns_alias = "coredns-model"

    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(cluster_kubeconfig))

    log.info("Adding k8s cloud")
    k8s_cloud = await ops_test.add_k8s(skip_storage=True, kubeconfig=config)
    k8s_model = await ops_test.track_model(
        coredns_alias, cloud_name=k8s_cloud, keep=ops_test.ModelKeep.NEVER
    )
    await k8s_model.deploy("coredns", trust=True)
    await k8s_model.wait_for_idle(apps=["coredns"], status="active")
    yield k8s_model

    # the cluster is consuming this model: remove saas first
    await ops_test.forget_model(coredns_alias, timeout=40)


@pytest_asyncio.fixture(scope="module")
async def integrate_coredns(ops_test: OpsTest, coredns_model: juju.model.Model, kubernetes_cluster: juju.model.Model):
    """
    This function offers Coredns in the specified Kubernetes (k8s) model.
    """
    log.info("Offering Coredns...")
    await coredns_model.create_offer("coredns:dns-provider")
    await coredns_model.block_until(lambda: 'coredns' in coredns_model.application_offers)
    log.info("Coredns offered...")

    log.info("Consuming Coredns...")
    model_owner = untag("user-", coredns_model.info.owner_tag)
    
    await coredns_model.wait_for_idle(status="active")
    await kubernetes_cluster.wait_for_idle(status="active")    

    offer_url = f"{model_owner}/{coredns_model.name}.coredns"
    saas = await kubernetes_cluster.consume(offer_url)

    log.info("Coredns consumed...")
    
    log.info("Relating Coredns...")
    await kubernetes_cluster.integrate("k8s:dns-provider", "coredns")
    assert "coredns" in kubernetes_cluster.remote_applications
    
    yield
    
    # Now let's clean up
    cluster = kubernetes_cluster
    await kubernetes_cluster.applications["k8s"].destroy_relation("k8s:dns-provider", "coredns")
    await kubernetes_cluster.wait_for_idle(status="active")  
    await kubernetes_cluster.remove_saas(saas)
    await coredns_model.remove_offer(f"{coredns_model.name}.{saas}", force=True)
    
   
