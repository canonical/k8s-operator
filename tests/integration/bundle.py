# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Rendering of the test bundles declared by the ``bundle`` marker."""

import logging
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import jubilant
import pytest
import yaml
from helpers import stage, wait_idle
from literals import CHARMCRAFT_DIRS, DEFAULT_SERIES, SERIES_VERSION, TEST_DATA

log = logging.getLogger(__name__)


@dataclass
class Markings:
    """Test markings for the bundle.

    Attrs:
        series: Series for machines in the bundle.
        apps_local: Application names that must be replaced by locally built charms.
        apps_channel: Mapping of application name to charm channel.
        apps_resources: Mapping of application name to resources.
    """

    series: Optional[str] = None
    apps_local: List[str] = field(default_factory=list)
    apps_channel: Mapping = field(default_factory=dict)
    apps_resources: Mapping = field(default_factory=dict)


@dataclass
class Charm:
    """Represents a charm whose source lives in this repository.

    Attrs:
        path: Path to the charmcraft project directory.
        metadata: Parsed charmcraft.yaml.
        name: Name of the charm from the metadata.
        local_path: Path to the built charm file.
    """

    path: Path
    _charmfile: Optional[Path] = None

    @cached_property
    def metadata(self) -> dict:
        """Charm metadata.

        Returns:
            The parsed charmcraft.yaml.
        """
        return yaml.safe_load((self.path / "charmcraft.yaml").read_text())

    @property
    def name(self) -> str:
        """Name defined by the charm.

        Returns:
            The charm name.
        """
        return self.metadata["name"]

    @property
    def local_path(self) -> Path:
        """Local path to the built charm.

        Returns:
            Path to the built charm file.

        Raises:
            FileNotFoundError: if the charm file wasn't found.
        """
        if self._charmfile is None:
            raise FileNotFoundError(f"{self.name}_*.charm not found")
        return self._charmfile

    @classmethod
    def find(cls, charm: str) -> Optional["Charm"]:
        """Find a charm managed in this repo based on its name.

        Args:
            charm: Charm name as it appears in the bundle.

        Returns:
            A Charm object, or None if the charm isn't built from this repository.
        """
        if charmcraft := CHARMCRAFT_DIRS.get(charm):
            return cls(charmcraft)
        return None

    def resolve(self, charm_files: Iterable[str], arch: str, base: str) -> "Charm":
        """Locate an already-built charm file, or build one.

        Args:
            charm_files: Paths passed with ``--charm-file``.
            arch: Cloud architecture, for example ``amd64``.
            base: Base version the charm must support, for example ``24.04``.

        Returns:
            self, with ``local_path`` resolved.

        Raises:
            FileNotFoundError: if no unambiguous charm file could be found or built.
        """
        prefix = f"{self.name}_"

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

        charm_name = prefix + "*.charm"
        if self._charmfile is None:
            try:
                potentials = chain(
                    map(Path, charm_files),  # Passed with --charm-file
                    Path().glob(charm_name),  # Top-level path
                    self.path.glob(charm_name),  # Charm-level path
                )
                self._charmfile = _narrow(potentials)
                log.info("For %s found charmfile %s", self.name, self._charmfile)
            except FileNotFoundError as err:
                log.warning(
                    "For %s failed locating existing charm (%s), build instead", self.name, err
                )

        if self._charmfile is None:
            log.info("For %s building charmfile with charmcraft", self.name)
            subprocess.run(["charmcraft", "pack", "-p", str(self.path)], check=True)
            # charmcraft writes into the current working directory, but older versions
            # wrote next to the project; look in both.
            potentials = chain(Path().glob(charm_name), self.path.glob(charm_name))
            self._charmfile = _narrow(potentials)
            log.info("For %s built charmfile %s", self.name, self._charmfile)

        return self


@dataclass
class Bundle:
    """Represents a test bundle.

    Attrs:
        path: Path to the bundle file.
        arch: Cloud architecture.
        series: Series for machines in the bundle.
        stage_dir: Subdirectory of the render directory to stage local files into.
        content: Parsed bundle content.
        applications: Mapping of applications in the bundle.
        needs_trust: Whether the bundle needs to be deployed with ``--trust``.
    """

    path: Path
    arch: str
    series: Optional[str] = None
    stage_dir: str = ""
    _content: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, request: pytest.FixtureRequest, arch: str) -> Tuple["Bundle", Markings]:
        """Build a Bundle from the test module's ``bundle`` marker.

        Args:
            request: Pytest fixture request.
            arch: Cloud architecture.

        Returns:
            The bundle and the markings taken from the marker.
        """
        bundle_marker = request.node.get_closest_marker("bundle")
        assert bundle_marker, "No bundle marker found"
        kwargs = {**bundle_marker.kwargs}

        if val := kwargs.pop("file", None):
            path = TEST_DATA / val
        else:
            log.warning("No file specified, using default test-bundle.yaml")
            path = TEST_DATA / "test-bundle.yaml"

        assert arch, "Architecture must be known before customizing the bundle"
        assert not all(_ in kwargs for _ in ("apps_local", "apps_channel")), (
            "Cannot use both apps_local and apps_channel"
        )

        bundle = cls(
            path=path,
            arch=arch,
            series=request.config.option.series,
            stage_dir=request.module.__name__,
        )
        return bundle, Markings(**kwargs)

    @property
    def content(self) -> Dict[str, Any]:
        """Bundle content, loaded on first access.

        Returns:
            The parsed bundle.
        """
        if not self._content:
            loaded = yaml.safe_load(self.path.read_bytes())
            self.series = self.series or loaded.get("series")
            self._content = loaded
        return self._content

    @property
    def applications(self) -> Mapping[str, dict]:
        """Applications defined by the bundle.

        Returns:
            Mapping of application name to application details.
        """
        return self.content["applications"]

    @property
    def needs_trust(self) -> bool:
        """Whether the bundle needs to be trusted.

        Returns:
            True if any application in the bundle requires trust.
        """
        return any(app.get("trust", False) for app in self.applications.values())

    def discover_charm_files(self, charm_files: Iterable[str]) -> Dict[str, Charm]:
        """Resolve the local charm files for applications built from this repository.

        Args:
            charm_files: Paths passed with ``--charm-file``.

        Returns:
            Mapping of charm name to Charm object.
        """
        charm_files = list(charm_files)
        # Touch `applications` first: loading the content is what picks up the bundle's own
        # top-level `series:` when neither --series nor a marking overrode it.
        applications = self.applications
        base = SERIES_VERSION[self.series or DEFAULT_SERIES]
        app_to_charm = {}
        for app in applications.values():
            if charm := Charm.find(str(app["charm"])):
                charm.resolve(charm_files, self.arch, base)
                app_to_charm[charm.name] = charm
        return app_to_charm

    def apply_marking(
        self,
        markings: Markings,
        *,
        provider: str,
        vms: bool,
        charm_files: Iterable[str],
        snap_resource: str,
        series: Optional[str] = None,
    ) -> None:
        """Customize the bundle for the test.

        Args:
            markings: Markings taken from the ``bundle`` marker.
            provider: Cloud provider type, from ``cloud.cloud_type``.
            vms: Whether the cloud provisions VMs.
            charm_files: Paths passed with ``--charm-file``.
            snap_resource: Path to the snap-installation resource tarball.
            series: Series override from ``--series``.
        """
        if provider == "lxd" and not vms:
            log.info("Drop lxd machine constraints")
            self.drop_constraints()
        if provider == "lxd" and vms:
            log.info("Constrain lxd machines with virt-type: virtual-machine")
            self.add_constraints({"virt-type": "virtual-machine"})

        if series := series or markings.series:
            self.content["series"] = self.series = series

        charms = self.discover_charm_files(charm_files)
        empty_resource = {"snap-installation": str(stage(Path(snap_resource), self.stage_dir))}

        for app in markings.apps_local:
            assert app in charms, f"App={app} doesn't have a local charm"
            rsc = markings.apps_resources.get(app) or empty_resource
            self.switch(app, charm=charms[app], channel=None, resources=rsc)

        for app, channel in markings.apps_channel.items():
            assert app in charms, f"App={app} isn't built from this repository"
            rsc = markings.apps_resources.get(app)
            self.switch(app, charm=charms[app], channel=channel, resources=rsc)

    def switch(
        self,
        name: str,
        charm: Charm,
        channel: Optional[str] = None,
        resources: Optional[dict] = None,
    ) -> None:
        """Replace a Charmhub application with a local charm file or a specific channel.

        Args:
            name: Application name.
            charm: Charm to use.
            channel: If specified use this channel, otherwise use the local charm file.
            resources: Optional resources to attach.
        """
        app = self.applications.get(name)
        if not app:
            return  # Skip if the application is not in the bundle
        if channel:
            app["charm"] = charm.name
            app["channel"] = channel
        else:
            # The juju CLI opens this path itself, so it must be readable by the juju snap.
            app["charm"] = str(stage(charm.local_path, self.stage_dir))
            app["channel"] = None
        if resources:
            app["resources"] = resources

    def drop_constraints(self) -> None:
        """Remove constraints from all applications. Useful for testing on LXD containers."""
        for app in self.applications.values():
            app["constraints"] = ""

    def add_constraints(self, constraints: Dict[str, str]) -> None:
        """Add constraints to the principal applications of the bundle.

        Args:
            constraints: Constraints to add.
        """
        for app in self.applications.values():
            if app.get("num_units", 0) < 1:
                log.info("Skipping constraints for subordinate charm: %s", app["charm"])
                continue
            existing = dict(
                kv.split("=", 1) for kv in str(app.get("constraints", "")).split() if "=" in kv
            )
            existing.update(constraints)
            app["constraints"] = " ".join(f"{k}={v}" for k, v in existing.items())

    def render(self, dest_dir: Path) -> Path:
        """Write the customized bundle out for deployment.

        Args:
            dest_dir: Directory to write the bundle into.

        Returns:
            Path to the written bundle.
        """
        self.add_constraints({"arch": self.arch})
        target = dest_dir / "bundles" / self.path.name
        target.parent.mkdir(exist_ok=True, parents=True)
        with target.open("w") as file:
            yaml.dump(self.content, file)
        log.info("Rendered bundle %s", target)
        return target

    def is_deployed(self, juju: jubilant.Juju, timeout: float) -> bool:
        """Check whether the model already holds every application of the bundle.

        If all applications are deployed with enough units, wait for the model to settle.

        Args:
            juju: Jubilant Juju instance.
            timeout: Timeout in seconds for the settle wait.

        Returns:
            True if the model can be reused as-is.
        """
        apps = yaml.safe_load(self.path.read_bytes())["applications"]
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
        wait_idle(juju, timeout=timeout)
        return True
