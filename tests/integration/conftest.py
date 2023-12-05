# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""
import asyncio
import contextlib
from dataclasses import asdict, dataclass
from itertools import chain
from pathlib import Path
from typing import Optional

import pytest_asyncio
import yaml
from pytest_operator.plugin import OpsTest


def pytest_addoption(parser):
    """Parse additional pytest options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", action="store")


@dataclass
class CharmDeploymentArgs:
    """Represents juju deploy arguments

    Attrs:
        entity_url:        url to charm deployment
        application_name:  name of juju application
        resources:         all resources for this charm
        series:            os series for the machine
    """

    entity_url: str
    application_name: str
    resources: dict
    series: str


@dataclass
class Charm:
    """Represents source charms.

    Attrs:
        ops_test:  Instance of the pytest-operator plugin
        path:      Path to the charm file
        metadata:  Charm's metadata
        app_name:  Preferred name of the juju application
        resources: dict of possible charm resources
    """

    ops_test: OpsTest
    path: Path
    _charmfile: Optional[Path] = None

    @property
    def metadata(self) -> dict:
        """Charm Metadata."""
        return yaml.safe_load((self.path / "metadata.yaml").read_text())

    @property
    def app_name(self) -> str:
        """Suggested charm name."""
        return self.metadata["name"]

    @property
    def resources(self) -> dict:
        """Charm resources."""
        resources = self.metadata.get("resources") or {}

        return {name: self._craft_resource(name, resource) for name, resource in resources.items()}

    def _craft_resource(self, _name: str, resource: dict):
        """Build resource from metadata item.

        Args:
            _name:    name of the resource
            resource: value for the resource during deployment

        Return:
            upstream-source for the resource if oci-image
            path to file if resource is a filepath
            None otherwise
        """
        if oci_image := resource.get("upstream-source"):
            return oci_image
        return None

    async def resolve(self) -> Path:
        """Build the charm with ops_test.

        Return:
            path to charm file

        Raises:
            FileNotFoundError: the charm file wasn't found
        """
        if self._charmfile is None:
            try:
                charm_name = f"{self.app_name}*.charm"
                potentials = chain(*(path.glob(charm_name) for path in (Path(), self.path)))
                self._charmfile, *_ = filter(None, potentials)
            except ValueError:
                self._charmfile = await self.ops_test.build_charm(self.path)
        if self._charmfile is None:
            raise FileNotFoundError(f"{self.app_name}*.charm not found")
        return self._charmfile.resolve()


@contextlib.asynccontextmanager
async def deploy_model(request, ops_test, model_name, *deploy_args: CharmDeploymentArgs):
    """Add a juju model, deploy apps into it, wait for them to be active.

    Args:
        request:     handle to pytest requests from calling fixture
        ops_test:    Instance of the pytest-operator plugin
        model_name:  name of the model in which to deploy
        deploy_args: list of charms to deploy and their arguments

    Yields:
        model object
    """
    config = {}
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
        await asyncio.gather(
            *(
                the_model.deploy(**asdict(charm))
                for charm in deploy_args
                if charm.application_name not in the_model.applications
            ),
            the_model.wait_for_idle(
                apps=[n.application_name for n in deploy_args],
                status="active",
                raise_on_blocked=True,
                timeout=15 * 60,
            ),
        )
        yield the_model


@pytest_asyncio.fixture(scope="module")
async def kubernetes_cluster(request, ops_test):
    """Deploy local kubernetes charms."""
    model = "main"
    charm_names = ("k8s", "k8s-worker")
    charms = [Charm(ops_test, Path("charms") / _) for _ in charm_names]
    charm_files = await asyncio.gather(*[charm.resolve() for charm in charms])
    deployments = [
        CharmDeploymentArgs(
            entity_url=str(path),
            application_name=charm.app_name,
            resources=charm.resources,
            series="jammy",
        )
        for path, charm in zip(charm_files, charms)
    ]
    async with deploy_model(request, ops_test, model, *deployments) as the_model:
        yield the_model
