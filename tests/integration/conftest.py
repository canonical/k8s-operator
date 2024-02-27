# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""
import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import List, Mapping, Optional
import juju.utils
import shlex
import pytest
import pytest_asyncio
import yaml
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
        async with ops_test.fast_forward():
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
    model = "main"
    charm_path = ("worker/k8s", "worker")
    charms = [Charm(ops_test, Path("charms") / p) for p in charm_path]
    charm_files = await asyncio.gather(
        *[charm.resolve(request.config.option.charm_files) for charm in charms]
    )

    bundle = Bundle(ops_test, Path(__file__).parent / "test-bundle.yaml")
    for path, charm in zip(charm_files, charms):
        bundle.switch(charm.app_name, path)

    async with deploy_model(request, ops_test, model, bundle) as the_model:
        yield the_model
    
    # Creating Kubernetes cloud
    await configure_k8s_cloud(ops_test)

    # Creating Coredns model
    coredns_model_result = await coredns_model(ops_test)

    # Deploying Coredns charm
    await manage_coredns_lifecycle(ops_test, coredns_model_result)

@pytest.fixture(scope="module")
async def configure_k8s_cloud(ops_test: OpsTest, k8s_cloud_name: str = "k8s-cloud"):
    """
    This fixture manages the Kubernetes (k8s) cloud for testing purposes.
    It adds the k8s cloud and later removes it.
    """
    controller = await ops_test.model.get_controller()
    try:
        current_clouds = await controller.clouds()
        if k8s_cloud_name in current_clouds.clouds:
            yield k8s_cloud_name
            return
    finally:
        await controller.disconnect()

    with ops_test.model_context("main"):
        log.info(f"Adding cloud '{k8s_cloud_name}'...")
        await ops_test.juju(
            "add-k8s",
            k8s_cloud_name,
            f"--controller={ops_test.controller_name}",
            "--client",
            "--skip-storage",
            check=True,
            fail_msg=f"Failed to add-k8s {k8s_cloud_name}",
        )
    yield k8s_cloud_name

    with ops_test.model_context("main"):
        log.info(f"Removing cloud '{k8s_cloud_name}'...")
        await ops_test.juju(
            "remove-cloud",
            k8s_cloud_name,
            "--controller",
            ops_test.controller_name,
            "--client",
            check=True,
        )

@pytest.fixture(scope="module")
async def coredns_model(k8s_cloud, ops_test: OpsTest):
    """
    This fixture sets up a Coredns model on the specified Kubernetes (k8s) cloud for testing purposes.
    It adds the k8s model, performs necessary operations, and removes the model after the test.
    """
    model_alias = "coredns-model"
    log.info("Creating Coredns model ...")

    model_name = "coredns-system"
    await ops_test.juju(
        "add-model",
        f"--controller={ops_test.controller_name}",
        model_name,
        k8s_cloud,
        "--no-switch", #TODO: does not switch to the new model, does that bite me in the next call to juju?
    )

    model = await ops_test.track_model(
        model_alias,
        model_name=model_name,
        cloud_name=k8s_cloud,
        # credential_name=k8s_cloud, #TODO: is this necessary?
        keep=False,
    )
    model_uuid = model.info.uuid

    yield model, model_alias

    timeout = 5 * 60
    await ops_test.forget_model(model_alias, timeout=timeout, allow_failure=False)

    async def model_removed():
        _, stdout, stderr = await ops_test.juju("models", "--format", "yaml")
        if _ != 0:
            return False
        model_list = yaml.safe_load(stdout)["models"]
        which = [m for m in model_list if m["model-uuid"] == model_uuid]
        return len(which) == 0

    log.info("Removing Coredns model")
    await juju.utils.block_until_with_coroutine(model_removed, timeout=timeout)
    # Update client's model cache
    await ops_test.juju("models")
    log.info("Coredns model removed ...")

@pytest.fixture(scope="module")
async def manage_coredns_lifecycle(ops_test: OpsTest, coredns_model):
    """
    This fixture deploys Coredns on the specified Kubernetes (k8s) model for testing purposes.
    It waits for the deployment to complete and ensures that the Coredns application is active.
    """
    log.info(f"Deploying Coredns ")

    #TODO: check what k8s_alias is and what it should be
    k8s_alias = coredns_model 
    with ops_test.model_context(k8s_alias) as model:
        await asyncio.gather(
            model.deploy(entity_url="coredns", trust=True, channel="edge", ),
        )

        await model.block_until(
            lambda: all("coredns" in model.applications),
            timeout=60,
        )
        await model.wait_for_idle(status="active", timeout=5 * 60)

        coredns_app = model.applications["coredns"]

        # Consume and relate Coredns
        await consume_core_dns(ops_test, cluster_model="your_cluster_model_name", k8s_model="k8s-model")

        await ops_test.model.wait_for_idle(status="active", timeout=5 * 60)

    yield

    with ops_test.model_context(k8s_alias) as m:
        log.info("Removing Coredns charm...")

        log.info(f"Removing coredns ...")
        cmd = "remove-application coredns --destroy-storage --force"
        rc, stdout, stderr = await ops_test.juju(*shlex.split(cmd))
        log.info(f"{(stdout or stderr)})")
        assert rc == 0
        await m.block_until(lambda: "coredns" not in m.applications, timeout=60 * 10)

async def consume_core_dns(ops_test: OpsTest, cluster_model: str, k8s_model: str = "k8s-model"):
    """
    This function consumes and relates Coredns in the specified Kubernetes (k8s) model with a cluster model.
    """
    log.info("Consuming Coredns...")
    # TODO: find out if ops test has something that does this?
    
    # Juju consume command
    consume_cmd = f"juju consume -m {cluster_model} {k8s_model}.coredns"
    rc, stdout, stderr = await ops_test.juju(*shlex.split(consume_cmd))
    log.info(f"{(stdout or stderr)}")
    assert rc == 0, "Failed to consume Coredns in the cluster model."
    # TODO: or wait for the relation to be established?
    await ops_test.model.wait_for_idle(status="active", timeout=5 * 60) 

    # Juju relate command
    relate_cmd = f"juju relate -m {cluster_model} coredns k8s"
    rc, stdout, stderr = await ops_test.juju(*shlex.split(relate_cmd))
    log.info(f"{(stdout or stderr)}")
    assert rc == 0, "Failed to relate Coredns in the cluster model with k8s"