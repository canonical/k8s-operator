# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""

import contextlib
import json
import logging
import random
import shlex
import string
from pathlib import Path
from typing import Generator, Optional

import juju.controller
import juju.utils
import kubernetes.client.models as k8s_models
import pytest
import yaml
from cos_substrate import COSSubstrate
from helpers import Bundle, cloud_type, get_kubeconfig, get_unit_cidrs
from juju.model import Model
from juju.tag import untag
from juju.url import URL
from kubernetes import config as k8s_config
from kubernetes.client import ApiClient, Configuration, CoreV1Api
from literals import ONE_MIN
from lxd_substrate import LXDSubstrate, VMOptions

# Enable pytest-jubilant plugin
pytest_plugins = ["pytest_jubilant"]

log = logging.getLogger(__name__)
TEST_DATA = Path(__file__).parent / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"
METRICS_AGENTS = ["grafana-agent:1/stable", "opentelemetry-collector:2/edge"]


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    --apply-proxy
        Apply proxy to model-config.
    --arch
        Only run tests matching this architecture (e.g., amd64 or arm64).
    --charm-file
        Can be used multiple times, specifies which local charm files are available.
        Expected filename format: {charmName}_{base}-{arch}.charm
        Example: k8s-worker_ubuntu-22.04-amd64_ubuntu-24.04-amd64.charm
        Some tests use subordinate charms (e.g. Ceph) that expect the charm
        base to match.
    --lxd-containers
        If cloud is LXD, use containers instead of LXD VMs.
        Note that some charms may not work in LXD containers (e.g. Ceph).
    --series
        Ubuntu series to deploy, overrides any markings.
    --snap-installation-resource
        Path to the snap installation resource.
        The tarball must contain either a "snap_installation.yaml" OR a
        "*.snap" file.
    --timeout
        Set timeout for tests
    --upgrade-from
        Instruct tests to start with a specific channel, and upgrade to these charms.

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
            "Example: k8s-worker_ubuntu-22.04-amd64_ubuntu-24.04-amd64.charm. "
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
    parser.addoption("--timeout", default=10, type=int, help="timeout for tests in minutes")
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
            "skipping all others (e.g., grafana-agent:1/stable or opentelemetry-collector:1/edge)."
        ),
    )


def pytest_collection_modifyitems(config, items):
    """Remove from selected tests based on config.

    Called after collection has been performed. May filter or re-order the items in-place.

    Args:
        config (pytest.Config): The pytest config object.
        items (List[pytest.Item]): List of item objects.
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
            # Test is marked with an architecture but the filter does not match.
            deselected.append(item)
        else:
            selected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


def cloud_proxied(jubilant):
    """Set up a cloud proxy settings if necessary.

    If ghcr.io is reachable through a proxy apply expected proxy config to juju model.

    Args:
        jubilant: jubilant plugin fixture
    """
    assert jubilant.model, "Model must be present"
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    controller: juju.controller.Controller = jubilant.model.get_controller()
    controller_model = controller.get_model("controller")
    proxy_config_file = TEST_DATA / "static-proxy-config.yaml"
    proxy_configs = yaml.safe_load(proxy_config_file.read_text())
    local_no_proxy = get_unit_cidrs(controller_model, "controller", 0)
    no_proxy = {*proxy_configs["juju-no-proxy"], *local_no_proxy}
    proxy_configs["juju-no-proxy"] = ",".join(sorted(no_proxy))
    jubilant.model.set_config(proxy_configs)


def cloud_profile(jubilant):
    """Apply Cloud Specific Settings to the model.

    Args:
        jubilant: jubilant plugin fixture
    """
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    _type, _vms = cloud_type(jubilant)
    if _type == "lxd" and jubilant.model:
        # lxd-profile to the model if the juju cloud is lxd.
        lxd = LXDSubstrate()

        lxd_profiles, lxd_networks = [], []
        # -- Setup LXD networks and profiles for the model.
        cloud_mark = jubilant.request.node.get_closest_marker("clouds")
        if cloud_mark and "lxd" in cloud_mark.args:
            if networks := cloud_mark.kwargs.get("networks"):
                lxd_networks.extend(networks)
            if profiles := cloud_mark.kwargs.get("profiles"):
                lxd_profiles.extend(profiles)

        profile_name = f"juju-{jubilant.model.name}-{jubilant.model.uuid[:6]}"
        lxd.configure_networks(lxd_networks)
        lxd.remove_profile(profile_name)
        lxd.apply_profile(lxd_profiles, profile_name)

    elif _type in ("ec2", "openstack") and jubilant.model:
        jubilant.model.set_config({"container-networking-method": "local", "fan-config": ""})


@pytest.fixture(scope="module", autouse=True)
def skip_by_cloud_type(request, jubilant):
    """Skip tests based on cloud type."""
    if cloud_markers := request.node.get_closest_marker("clouds"):
        # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
        _type, _ = cloud_type(jubilant)
        if _type not in cloud_markers.args:
            pytest.skip(f"cloud={_type} not among {cloud_markers.args}")


@contextlib.contextmanager
def deploy_model(
    jubilant,
    model_name: str,
    bundle: Bundle,
):
    """Add a juju model, deploy apps into it, wait for them to be active.

    Args:
        jubilant:          Instance of the pytest-jubilant plugin
        model_name:        name of the model in which to deploy
        bundle:            Bundle object to deploy or redeploy into the model

    Yields:
        model object
    """
    config: Optional[dict] = {}
    at_least_60 = max(60, jubilant.request.config.option.timeout)
    if jubilant.request.config.option.model_config:
        config = jubilant.read_model_config(jubilant.request.config.option.model_config)
    credential_name = jubilant.cloud_name
    if model_name not in jubilant.models:
        # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
        jubilant.track_model(
            model_name,
            model_name=model_name,
            credential_name=credential_name,
            config=config,
        )
    with jubilant.model_context(model_name) as the_model:
        cloud_profile(jubilant)
        # MIGRATION: switched to non-async context manager (jubilant)
        with jubilant.fast_forward(ONE_MIN):
            bundle_yaml = bundle.render(jubilant.tmp_path)
            the_model.deploy(bundle_yaml, trust=bundle.needs_trust)
            the_model.wait_for_idle(
                apps=list(bundle.applications),
                status="active",
                timeout=at_least_60 * 60,
            )
        try:
            yield the_model
        except GeneratorExit:
            log.fatal("Failed to determine model: model_name=%s", model_name)


@pytest.fixture(scope="module")
def kubernetes_cluster(request: pytest.FixtureRequest, jubilant) -> Generator[Model, None, None]:
    """Deploy kubernetes charms according to the bundle_marker."""
    model = "main"
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    bundle, markings = Bundle.create(jubilant)

    with jubilant.model_context(model) as the_model:
        if bundle.is_deployed(the_model):
            log.info("Using existing model=%s.", the_model.uuid)
            yield the_model
            return

    if request.config.option.no_deploy:
        pytest.skip("Skipping because of --no-deploy")

    log.info("Deploying new cluster using %s bundle.", bundle.path)
    if request.config.option.apply_proxy:
        cloud_proxied(jubilant)

    bundle.apply_marking(jubilant, markings)
    with deploy_model(jubilant, model, bundle) as the_model:
        yield the_model


def valid_namespace_name(s: str) -> str:
    """Create a valid kubernetes namespace name.

    Args:
        s: The string to sanitize.

    Returns:
        A valid namespace name.
    """
    valid_chars = set(string.ascii_lowercase + string.digits + "-")
    sanitized = "".join("-" if char not in valid_chars else char for char in s)
    sanitized = sanitized.strip("-")
    return sanitized[-63:]


@pytest.fixture(scope="module")
def api_client(
    kubernetes_cluster,
    jubilant,
    request,  # pylint: disable=unused-argument
):
    """Create a k8s API client and namespace for the test.

    Args:
        kubernetes_cluster: The k8s model.
        jubilant: The pytest-jubilant plugin.
        request: The pytest request object.
    """
    module_name = request.module.__name__
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = valid_namespace_name(f"{module_name}-{rand_str}")
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    kubeconfig_path = get_kubeconfig(jubilant, module_name)
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))
    client = ApiClient(configuration=config)

    v1 = CoreV1Api(client)
    v1.create_namespace(
        body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
    )
    yield client
    v1.delete_namespace(name=namespace)


@pytest.fixture(scope="module", params=METRICS_AGENTS)
def metrics_agent(kubernetes_cluster: Model, request):
    """Deploy Metrics Agent Charm."""
    apps = ["k8s", "k8s-worker"]
    option = request.config.option.metrics_agent_charm
    if option and option not in request.param:
        pytest.skip(
            f"Skipping metrics agent charm {request.param} due to --metrics-agent-charm={option}"
        )

    metrics_agent, metrics_agent_channel = request.param.split(":")
    k8s, worker = (kubernetes_cluster.applications.get(a) for a in apps)
    if not k8s:
        pytest.fail("k8s application not found in the model")
    data = k8s.units[0].machine.safe_data
    arch = data["hardware-characteristics"]["arch"]
    series = juju.utils.get_version_series(data["base"].split("@")[1])
    url = URL("ch", name=metrics_agent, series=series, architecture=arch)

    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    kubernetes_cluster.deploy(url, channel=metrics_agent_channel, series=series)
    kubernetes_cluster.integrate(f"{metrics_agent}:cos-agent", "k8s:cos-agent")
    if worker:
        kubernetes_cluster.integrate(f"{metrics_agent}:cos-agent", "k8s-worker:cos-agent")
        kubernetes_cluster.integrate("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")

    yield metrics_agent

    kubernetes_cluster.remove_application(metrics_agent)
    if worker:
        kubernetes_cluster.applications["k8s"].destroy_relation(
            "cos-worker-tokens", "k8s-worker:cos-tokens", block_until_done=True
        )


@pytest.fixture(scope="module")
def cos_model(jubilant, kubernetes_cluster, metrics_agent):
    """Create a COS substrate and a K8s model."""
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    _type, _vms = cloud_type(jubilant)
    assert _type == "lxd", "COS tests only supported on LXD clouds"

    manager = COSSubstrate(VMOptions() if _vms else None)
    config = manager.create_substrate()
    kubeconfig_path = jubilant.tmp_path / "kubeconfig"
    kubeconfig_path.write_bytes(config)
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))

    k8s_cloud = jubilant.add_k8s(kubeconfig=config, skip_storage=False)
    k8s_model = jubilant.track_model("cos", cloud_name=k8s_cloud, keep=jubilant.ModelKeep.NEVER)
    yield k8s_model

    jubilant.forget_model("cos", timeout=10 * 60, allow_failure=True)

    manager.teardown_substrate()


@pytest.fixture(name="_cos_lite_installed", scope="module")
def cos_lite_installed(jubilant, cos_model: Model):
    """Install COS Lite bundle."""
    log.info("Deploying COS bundle ...")
    cos_charms = [
        "alertmanager",
        "catalogue",
        "grafana",
        "loki",
        "prometheus",
        "traefik",
    ]
    bundles = (
        jubilant.Bundle("cos-lite", "edge"),
        "tests/integration/data/cos-offers-overlay.yaml",
    )

    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    bundle, *overlays = jubilant.async_render_bundles(*bundles)
    cmd = f"juju deploy -m {cos_model.name} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )
    rc, stdout, stderr = jubilant.run(*shlex.split(cmd))
    assert rc == 0, f"COS Lite failed to deploy: {(stderr or stdout).strip()}"

    cos_model.block_until(
        lambda: all(app in cos_model.applications for app in cos_charms),
        timeout=5 * 60,
    )
    cos_model.wait_for_idle(status="active", timeout=20 * 60, raise_on_error=False)

    yield
    log.info("Removing COS Lite charms...")
    with jubilant.model_context("cos"):
        for charm in cos_charms:
            log.info("Removing %s...", charm)
            cmd = f"remove-application {charm} --destroy-storage --force --no-prompt"
            rc, stdout, stderr = jubilant.juju(*shlex.split(cmd))
            log.info("%s", stdout or stderr)
            assert rc == 0
        cos_model.block_until(
            lambda: all(app not in cos_model.applications for app in cos_charms),
            timeout=60 * 10,
        )


@pytest.fixture(scope="module")
def traefik_url(cos_model: Model, _cos_lite_installed):
    """Fixture to fetch Traefik url."""
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    action = cos_model.applications["traefik"].units[0].run_action("show-proxied-endpoints")
    action = action.wait()
    p_e = json.loads(action.results["proxied-endpoints"])

    yield p_e["traefik"]["url"]


@pytest.fixture(scope="module")
def expected_dashboard_titles():
    """Fixture to get expected Grafana dashboard titles."""
    grafana_dir = Path("charms/worker/k8s/src/grafana_dashboards")
    grafana_files = [p for p in grafana_dir.iterdir() if p.is_file() and p.name.endswith(".json")]
    titles = []
    for path in grafana_files:
        dashboard = json.loads(path.read_text())
        titles.append(dashboard["title"])
    return set(titles)


@pytest.fixture(name="_related_grafana", scope="module")
@pytest.mark.usefixtures("_cos_lite_installed")
def related_grafana(jubilant, cos_model: Model, metrics_agent):
    """Fixture to integrate with Grafana."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with jubilant.model_context("main") as model:
        log.info("Integrating with Grafana")
        # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
        model.integrate(
            metrics_agent,
            f"{model_owner}/{cos_model_name}.grafana-dashboards",
        )
        with jubilant.model_context("cos") as k8s_model:
            k8s_model.wait_for_idle(status="active")
        model.wait_for_idle(status="active")

    yield

    with jubilant.model_context("main") as model:
        log.info("Removing Grafana SAAS ...")
        model.remove_saas("grafana-dashboards")
    with jubilant.model_context("cos") as model:
        log.info("Removing Grafana Offer...")
        model.remove_offer(f"{model.name}.grafana-dashboards", force=True)


@pytest.fixture(scope="module")
def grafana_password(cos_model, _related_grafana):
    """Fixture to get Grafana password."""
    # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
    action = cos_model.applications["grafana"].units[0].run_action("get-admin-password")
    action = action.wait()
    yield action.results["admin-password"]


@pytest.fixture(scope="module")
@pytest.mark.usefixtures("_cos_lite_installed")
def related_prometheus(jubilant, cos_model, metrics_agent):
    """Fixture to integrate with Prometheus."""
    model_owner = untag("user-", cos_model.info.owner_tag)
    cos_model_name = cos_model.name

    with jubilant.model_context("main") as model:
        log.info("Integrating with Prometheus")
        # MIGRATION: removed await per jubilant; verify this method is sync in jubilant
        model.integrate(
            metrics_agent,
            f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
        )
        model.wait_for_idle(status="active")
        with jubilant.model_context("cos") as model:
            model.wait_for_idle(status="active")

    yield

    with jubilant.model_context("main") as model:
        log.info("Removing Prometheus Remote Write SAAS ...")
        model.remove_saas("prometheus-receive-remote-write")

    with jubilant.model_context("cos") as model:
        log.info("Removing Prometheus Offer...")
        model.remove_offer(f"{model.name}.prometheus-receive-remote-write", force=True)


@pytest.fixture(scope="module")
def timeout(request):
    """Fixture to set the timeout for certain tests."""
    return request.config.option.timeout
