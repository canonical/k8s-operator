# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for the Jubilant-based integration tests."""

import json
import logging
import random
import shlex
import shutil
import string
import subprocess
from pathlib import Path
from typing import Dict, Iterator, List

import jubilant
import kubernetes.client.models as k8s_models
import pytest
import yaml
from bundle import Bundle
from cloud import cloud_arch, cloud_profile, cloud_proxied, cloud_type
from helpers import fast_forward, get_kubeconfig, render_dir, wait_active
from kubernetes import config as k8s_config
from kubernetes.client import ApiClient, Configuration, CoreV1Api
from literals import DEFAULT_SNAP_INSTALLATION, ONE_MIN

log = logging.getLogger(__name__)


def _addoption_if_absent(parser: pytest.Parser, *args, **kwargs) -> None:
    """Register a CLI option unless another plugin already registered it.

    Registering an option a second time is a hard error, so options whose names are
    commonly owned by third-party plugins go through here. For example, pytest-timeout
    owns ``--timeout``, so installing it anywhere in the dependency tree would otherwise
    break collection outright.

    Args:
        parser: Pytest parser.
        args: Option strings.
        kwargs: Option keyword arguments.
    """
    try:
        parser.addoption(*args, **kwargs)
    except ValueError:
        log.debug("Option %s already registered by another plugin", args)


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption(
        "--apply-proxy", action="store_true", default=False, help="Apply proxy to model-config"
    )
    parser.addoption(
        "--arch",
        dest="arch",
        default=None,
        type=str,
        help="Only run tests matching this architecture (e.g., amd64 or arm64)",
    )
    parser.addoption(
        "--charm-file",
        dest="charm_files",
        action="append",
        default=[],
        help=(
            "Can be used multiple times, specifies which local charm files are available. "
            r"Expected filename format: {charmName}_{base}-{arch}.charm. "
            "Example: k8s-worker_ubuntu-24.04-amd64_ubuntu-24.04-amd64.charm. "
            "Some tests use subordinate charms (e.g. Ceph) that expect the charm "
            "base to match."
        ),
    )
    parser.addoption(
        "--lxd-containers",
        action="store_true",
        default=False,
        help=(
            "If cloud is LXD, use containers instead of LXD VMs. "
            "Note that some charms may not work in LXD containers (e.g. Ceph)."
        ),
    )
    parser.addoption("--series", default=None, help="Series to deploy, overrides any markings")
    parser.addoption(
        "--snap-installation-resource",
        default=str(DEFAULT_SNAP_INSTALLATION.resolve()),
        help=(
            "Path to the snap installation resource. "
            'The tarball must contain either a "snap_installation.yaml" OR a '
            '"*.snap" file.'
        ),
    )
    _addoption_if_absent(
        parser, "--timeout", default=90, type=int, help="timeout for tests in minutes"
    )
    parser.addoption(
        "--upgrade-from", dest="upgrade_from", default=None, help="Charms channel to upgrade from"
    )
    parser.addoption(
        "--metrics-agent-charm",
        dest="metrics_agent_charm",
        type=str,
        default="",  # empty string means all
        help=(
            "Run test_cos module only with this metrics agent charm, "
            "skipping all others (e.g., opentelemetry-collector:2/stable)."
        ),
    )

    # Option names that a third-party plugin may already own; see _addoption_if_absent.
    _addoption_if_absent(
        parser,
        "--model",
        action="store",
        default=None,
        help="Juju model to use; if not provided, a temporary model is created per module",
    )
    _addoption_if_absent(
        parser,
        "--no-deploy",
        action="store_true",
        default=False,
        help="Together with --model, skip deploying and reuse what is in the model",
    )
    _addoption_if_absent(
        parser,
        "--model-config",
        action="store",
        default=None,
        help="Path to a YAML file applied to the model on creation",
    )
    _addoption_if_absent(
        parser,
        "--keep-models",
        action="store_true",
        default=False,
        help="Keep the models created by the tests (maps to --no-juju-teardown)",
    )
    _addoption_if_absent(
        parser,
        "--crash-dump-args",
        action="store",
        default="",
        help="Extra arguments passed to juju-crashdump when a test fails",
    )


def pytest_configure(config: pytest.Config):
    """Validate option combinations and honour the --keep-models compatibility flag.

    Args:
        config: The pytest config object.
    """
    if config.getoption("--no-deploy") and config.getoption("--model") is None:
        raise pytest.UsageError("must specify --model when using --no-deploy")
    if config.getoption("--keep-models"):
        # operator-workflows always passes --keep-models. pytest-jubilant spells the same
        # intent --no-juju-teardown.
        config.option.no_juju_teardown = True


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]):
    """Deselect tests whose architecture marker doesn't match --arch.

    This runs during collection with no access to a Juju controller, so it must not talk
    to Juju.

    Args:
        config: The pytest config object.
        items: Collected items, filtered in place.
    """
    arch_filter = config.getoption("--arch")

    selected, deselected = [], []
    for item in items:
        if (
            (arch_mark := item.get_closest_marker("architecture"))
            and arch_filter
            and arch_mark.args
            and arch_filter not in arch_mark.args
        ):
            deselected.append(item)
        else:
            selected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(scope="module")
def timeout(request: pytest.FixtureRequest) -> int:
    """Return the --timeout value, in minutes.

    Args:
        request: Pytest fixture request.

    Returns:
        Timeout in minutes.
    """
    return request.config.option.timeout


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest, juju_factory) -> jubilant.Juju:
    """Module-scoped Juju instance, honouring --model.

    Shadows pytest-jubilant's ``juju`` fixture. When --model is given the model is neither
    created nor destroyed by the tests; this supports the nightly workflow, which deploys a
    cluster with terraform and then runs the tests against it.

    Args:
        request: Pytest fixture request.
        juju_factory: pytest-jubilant's temporary-model factory.

    Returns:
        A Jubilant Juju instance bound to the module's model.
    """
    if model := request.config.getoption("--model"):
        if request.config.getoption("--juju-dump-logs"):
            log.warning(
                "--model bypasses pytest-jubilant's model registry, so --juju-dump-logs "
                "will not write a debug-log for model %s",
                model,
            )
        return jubilant.Juju(model=model)
    instance = juju_factory.get_juju("")
    if request.config.getoption("--juju-switch"):
        assert instance.model
        instance.cli("switch", instance.model, include_model=False)
    return instance


@pytest.fixture(scope="module", autouse=True)
def skip_by_cloud_type(request: pytest.FixtureRequest, juju: jubilant.Juju):
    """Skip a module whose ``clouds`` marker doesn't include the current cloud.

    Args:
        request: Pytest fixture request.
        juju: Jubilant Juju instance.
    """
    if cloud_markers := request.node.get_closest_marker("clouds"):
        provider, _ = cloud_type(juju.model, request.config.option.lxd_containers)
        if provider not in cloud_markers.args:
            pytest.skip(f"cloud={provider} not among {cloud_markers.args}")


@pytest.fixture(scope="module")
def k8s_cluster(
    request: pytest.FixtureRequest,
    juju: jubilant.Juju,
    timeout: int,
    crash_dump: None,  # noqa: ARG001 -- ordering only: teardown must run after the tests
) -> Iterator[jubilant.Juju]:
    """Deploy the bundle named by the module's ``bundle`` marker and wait for it to settle.

    Args:
        request: Pytest fixture request.
        juju: Jubilant Juju instance owning the module's model.
        timeout: Timeout in minutes.
        crash_dump: Fixture that dumps diagnostics if the module had failures.

    Yields:
        The same Juju instance, once the cluster is up.
    """
    option = request.config.option
    at_least_60 = max(60, timeout)
    juju.wait_timeout = at_least_60 * 60

    provider, vms = cloud_type(juju.model, option.lxd_containers)
    bundle, markings = Bundle.create(request, cloud_arch(juju.show_model().controller_name))

    if bundle.is_deployed(juju, timeout=at_least_60 * 60):
        log.info("Using existing model %s.", juju.model)
        yield juju
        return

    if request.config.getoption("--no-deploy"):
        pytest.skip("Skipping because of --no-deploy")

    log.info("Deploying new cluster using %s bundle.", bundle.path)
    if option.apply_proxy:
        cloud_proxied(juju)

    if model_config := request.config.getoption("--model-config"):
        juju.model_config(yaml.safe_load(Path(model_config).read_text()))

    bundle.apply_marking(
        markings,
        provider=provider,
        vms=vms,
        charm_files=option.charm_files,
        snap_resource=option.snap_installation_resource,
        series=option.series,
    )
    cloud_profile(juju, provider, request.node.get_closest_marker("clouds"))

    # Render into a directory the juju CLI can read: when juju is a snap it cannot open
    # /tmp, and the bundle references local charm and resource paths that juju resolves
    # itself. One subdirectory per module so parallel modules can't collide.
    bundle_path = bundle.render(render_dir() / request.module.__name__)
    with fast_forward(juju, ONE_MIN):
        juju.deploy(bundle_path, trust=bundle.needs_trust)
        wait_active(juju, *bundle.applications, timeout=at_least_60 * 60)

    yield juju


@pytest.fixture(scope="module")
def crash_dump(request: pytest.FixtureRequest, juju: jubilant.Juju) -> Iterator[None]:
    """Run juju-crashdump against the module's model if any test in it failed.

    Must be torn down before the model is destroyed, which is why it is requested from
    ``k8s_cluster``
    (pytest tears fixtures down in reverse order of setup, and ``juju`` is set up first).

    Args:
        request: Pytest fixture request.
        juju: Jubilant Juju instance owning the module's model.

    Yields:
        None.
    """
    failed_before = request.session.testsfailed
    yield
    if request.session.testsfailed == failed_before:
        return
    if not shutil.which("juju-crashdump"):
        log.info("juju-crashdump command was not found.")
        return
    args = shlex.split(request.config.getoption("--crash-dump-args") or "")
    cmd = ["juju-crashdump", "-s", f"-m={juju.model}", "-a=debug-layer", "-a=config", *args]
    log.info("Running %s", shlex.join(cmd))
    result = subprocess.run(cmd, check=False)  # noqa: S603
    log.info("juju-crashdump finished [%s]", result.returncode)


def valid_namespace_name(name: str) -> str:
    """Sanitize a string into a valid Kubernetes namespace name.

    Args:
        name: The string to sanitize.

    Returns:
        A valid namespace name.
    """
    valid_chars = set(string.ascii_lowercase + string.digits + "-")
    sanitized = "".join("-" if char not in valid_chars else char for char in name)
    return sanitized.strip("-")[-63:]


@pytest.fixture(scope="module")
def api_client(
    k8s_cluster: jubilant.Juju,
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ApiClient]:
    """Create a Kubernetes API client and a namespace for the test module.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        request: Pytest fixture request.
        tmp_path_factory: Pytest temporary path factory.

    Yields:
        A Kubernetes ApiClient.
    """
    module_name = request.module.__name__
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = valid_namespace_name(f"{module_name}-{rand_str}")
    kubeconfig_path = get_kubeconfig(k8s_cluster, tmp_path_factory.mktemp(module_name))

    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))
    client = ApiClient(configuration=config)

    v1 = CoreV1Api(client)
    v1.create_namespace(
        body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
    )
    yield client
    v1.delete_namespace(name=namespace)


def user_config(juju: jubilant.Juju, app: str) -> Dict[str, str]:
    """Return the config keys of an application that were explicitly set by a user.

    ``jubilant.Juju.config()`` drops the ``source`` field, so it cannot distinguish a
    user-set value from a charm default. Go to the CLI for that.

    Args:
        juju: Jubilant Juju instance.
        app: Application name.

    Returns:
        Mapping of user-set config key to its value, as a string.
    """
    raw = json.loads(juju.cli("config", "--format", "json", app))
    return {
        key: str(value["value"])
        for key, value in raw["settings"].items()
        if value.get("source") == "user" and "value" in value
    }


@pytest.fixture
def preserve_charm_config(
    request: pytest.FixtureRequest, k8s_cluster: jubilant.Juju, timeout: int
):
    """Snapshot the user-set charm config of the module's apps and restore it afterwards.

    The module must define an ``APPS`` list naming the applications to preserve.

    Args:
        request: Pytest fixture request.
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        timeout: Timeout in minutes.

    Yields:
        Mapping of application name to its user-set config before the test.
    """
    apps: List[str] = list(getattr(request.module, "APPS", ["k8s"]))
    pre = {app: user_config(k8s_cluster, app) for app in apps}
    yield pre

    changed = False
    for app in apps:
        post = user_config(k8s_cluster, app)
        to_reset = sorted(set(post) - set(pre[app]))
        to_set = {k: v for k, v in pre[app].items() if post.get(k) != v}
        if to_reset:
            log.info("Resetting %s config keys %s", app, to_reset)
            k8s_cluster.config(app, reset=to_reset)
            changed = True
        if to_set:
            log.info("Restoring %s config keys %s", app, sorted(to_set))
            k8s_cluster.config(app, to_set)
            changed = True

    if changed:
        with fast_forward(k8s_cluster, ONE_MIN):
            wait_active(k8s_cluster, *apps, timeout=timeout * 60)
