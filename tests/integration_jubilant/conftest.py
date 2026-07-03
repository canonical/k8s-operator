# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for Jubilant-based k8s integration tests."""

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Iterator

import jubilant
import pytest
import yaml

log = logging.getLogger(__name__)

TEST_DATA = Path(__file__).parent.parent / "integration" / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"

_CHARMCRAFT_DIRS = {
    "k8s": Path("charms/worker/k8s"),
    "k8s-worker": Path("charms/worker"),
}


# CLI options


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register Jubilant-suite specific CLI options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption(
        "--charm-file",
        dest="charm_files",
        action="append",
        default=[],
        help=(
            "Path to a built charm file. Can be supplied multiple times. "
            "When set, charm-path fixtures use matching files instead of env vars or glob."
        ),
    )
    parser.addoption(
        "--snap-installation-resource",
        dest="snap_installation_resource",
        default=str(DEFAULT_SNAP_INSTALLATION.resolve()),
        help=(
            "Path to the snap installation resource tarball. "
            'Must contain a "snap_installation.yaml" OR a "*.snap" file.'
        ),
    )
    parser.addoption(
        "--series",
        default=None,
        help="Ubuntu series to deploy, overrides bundle default.",
    )
    parser.addoption(
        "--timeout",
        default=10,
        type=int,
        help="Timeout for tests, in minutes.",
    )
    parser.addoption(
        "--lxd-containers",
        action="store_true",
        default=False,
        help="If cloud is LXD, use containers instead of VMs.",
    )


# Charm-path helpers


def _find_charm(name: str, env_var: str, charm_files: list[str]) -> Path:
    """Return the path to a packed charm.

    Resolution order:
    1. ``--charm-file`` values passed by operator-workflows CI (filtered by name prefix).
    2. ``env_var`` environment variable.
    3. Glob for ``<name>_*.charm`` in the project root and the charm's own directory.

    Args:
        name:        Charm name (e.g. "k8s" or "k8s-worker").
        env_var:     Environment variable that may hold the path.
        charm_files: List of ``--charm-file`` values from pytest options.

    Returns:
        Resolved path to the .charm file.

    Raises:
        AssertionError: if no file is found or the result is ambiguous.
    """
    prefix = f"{name}_"

    # 1. --charm-file args (passed by operator-workflows)
    matching = [Path(f).resolve() for f in charm_files if Path(f).name.startswith(prefix)]
    if len(matching) == 1:
        assert matching[0].is_file(), f"--charm-file {matching[0]} is not a file"
        return matching[0]
    if len(matching) > 1:
        raise AssertionError(f"Multiple --charm-file values match '{name}': {matching}")

    # 2. Environment variable
    if path_str := os.environ.get(env_var):
        path = Path(path_str).resolve()
        assert path.is_file(), f"{env_var}={path_str!r} is not a file"
        return path

    # 3. Glob fallback
    pattern = f"{name}_*.charm"
    charmcraft_dir = _CHARMCRAFT_DIRS.get(name, Path())
    candidates = list(Path().glob(pattern)) + list(charmcraft_dir.glob(pattern))
    assert candidates, (
        f"No packed charm found for '{name}'. "
        f"Set {env_var}, pass --charm-file, or run 'charmcraft pack' first."
    )
    assert len(candidates) == 1, (
        f"Multiple charm files found for '{name}': {candidates}. "
        f"Set {env_var} or pass --charm-file to disambiguate."
    )
    return candidates[0].resolve()


@pytest.fixture(scope="session")
def k8s_charm_path(request: pytest.FixtureRequest) -> Path:
    """Path to the packed k8s charm.

    Resolved from ``--charm-file``, ``K8S_CHARM_PATH``, or glob.

    Args:
        request: Pytest fixture request.
    """
    return _find_charm("k8s", "K8S_CHARM_PATH", request.config.option.charm_files)


@pytest.fixture(scope="session")
def k8s_worker_charm_path(request: pytest.FixtureRequest) -> Path:
    """Path to the packed k8s-worker charm.

    Resolved from ``--charm-file``, ``K8S_WORKER_CHARM_PATH``, or glob.

    Args:
        request: Pytest fixture request.
    """
    return _find_charm("k8s-worker", "K8S_WORKER_CHARM_PATH", request.config.option.charm_files)


# Timeout fixture


@pytest.fixture(scope="module")
def timeout(request: pytest.FixtureRequest) -> int:
    """Return the --timeout value in minutes.

    Args:
        request: Pytest fixture request.
    """
    return request.config.option.timeout


# fast_forward context manager


@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, interval: str = "10s") -> Iterator[None]:
    """Temporarily speed up update-status hooks.

    Args:
        juju:     Jubilant Juju instance.
        interval: Hook interval while inside the context (default "10s").

    Yields:
        None
    """
    old = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": interval})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": old})


# Bundle rendering helpers


def _detect_arch(juju: jubilant.Juju) -> str:
    """Return the architecture of the Juju controller's machines.

    Tries model info first, falls back to show-controller, then defaults to amd64.

    Args:
        juju: Jubilant Juju instance.

    Returns:
        Architecture string (e.g. "amd64", "arm64").
    """
    model_info = juju.show_model()
    for machine in model_info.machines.values():
        hw = machine.hardware_characteristics  # type: ignore[attr-defined]
        arch = hw.get("arch", "") if isinstance(hw, dict) else getattr(hw, "arch", "") or ""
        if arch:
            return arch
    try:
        out = juju.cli("show-controller", "--format=json", include_model=False)
        for ctrl_data in json.loads(out).values():
            arch = (
                ctrl_data.get("details", {})
                .get("hardware-characteristics", {})
                .get("arch", "")
            )
            if arch:
                return arch
    except Exception:
        log.warning("Could not detect arch from controller; defaulting to amd64")
    return "amd64"


def _render_bundle(
    juju: jubilant.Juju,
    tmp_path: Path,
    k8s_charm: Path,
    k8s_worker_charm: Path,
    snap_resource: str,
    series: str | None,
    lxd_containers: bool,
) -> Path:
    """Render test-bundle.yaml with local charm paths and write it to *tmp_path*.

    Args:
        juju:             Jubilant Juju instance (used to detect cloud arch).
        tmp_path:         Temporary directory to write the rendered bundle.
        k8s_charm:        Local path to the k8s charm file.
        k8s_worker_charm: Local path to the k8s-worker charm file.
        snap_resource:    Path to the snap-installation resource tarball.
        series:           Override bundle series if set.
        lxd_containers:   If True on LXD, drop machine constraints.

    Returns:
        Path to the written bundle YAML.
    """
    bundle = yaml.safe_load((TEST_DATA / "test-bundle.yaml").read_bytes())

    if series:
        bundle["series"] = series

    arch = _detect_arch(juju)

    apps = bundle["applications"]
    apps["k8s"]["charm"] = str(k8s_charm)
    apps["k8s"]["channel"] = None
    apps["k8s"].setdefault("resources", {})["snap-installation"] = snap_resource
    apps["k8s-worker"]["charm"] = str(k8s_worker_charm)
    apps["k8s-worker"]["channel"] = None
    apps["k8s-worker"].setdefault("resources", {})["snap-installation"] = snap_resource

    for app in apps.values():
        if app.get("num_units", 0) < 1:
            continue
        if lxd_containers:
            app["constraints"] = ""
        else:
            val: str = app.get("constraints", "")
            existing = dict(kv.split("=", 1) for kv in val.split() if "=" in kv)
            existing["arch"] = arch
            app["constraints"] = " ".join(f"{k}={v}" for k, v in existing.items())

    target = tmp_path / "test-bundle.yaml"
    target.write_text(yaml.dump(bundle))
    return target


# k8s_cluster fixture


@pytest.fixture(scope="module")
def k8s_cluster(
    juju: jubilant.Juju,
    tmp_path_factory: pytest.TempPathFactory,
    k8s_charm_path: Path,
    k8s_worker_charm_path: Path,
    request: pytest.FixtureRequest,
    timeout: int,
) -> jubilant.Juju:
    """Deploy the k8s + k8s-worker bundle and wait for it to be active.

    The model is created and managed by pytest-jubilant's ``juju`` fixture.

    Args:
        juju:                  Jubilant Juju instance (module-scoped, from pytest-jubilant).
        tmp_path_factory:      Pytest tmp-path factory for writing the rendered bundle.
        k8s_charm_path:        Path to the packed k8s charm.
        k8s_worker_charm_path: Path to the packed k8s-worker charm.
        request:               Pytest fixture request.
        timeout:               Timeout in minutes from --timeout option.

    Returns:
        The same ``juju`` instance after the cluster is deployed.
    """
    juju.wait_timeout = timeout * 60

    snap_resource = request.config.option.snap_installation_resource
    series = request.config.option.series
    lxd_containers = request.config.option.lxd_containers

    tmp_path = tmp_path_factory.mktemp("bundle")
    bundle_path = _render_bundle(
        juju,
        tmp_path,
        k8s_charm_path,
        k8s_worker_charm_path,
        snap_resource,
        series,
        lxd_containers,
    )

    log.info("Deploying k8s bundle from %s", bundle_path)
    with fast_forward(juju):
        juju.deploy(bundle_path, trust=True)
        juju.wait(
            lambda s: jubilant.all_active(s, "k8s", "k8s-worker"),
            error=jubilant.any_error,
            timeout=timeout * 60,
        )

    return juju
