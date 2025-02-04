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
from typing import Optional

import juju.controller
import juju.utils
import kubernetes.client.models as k8s_models
import pytest
import pytest_asyncio
import yaml
from juju.model import Model
from juju.tag import untag
from juju.url import URL
from kubernetes import config as k8s_config
from kubernetes.client import ApiClient, Configuration, CoreV1Api
from pytest_operator.plugin import OpsTest

from .cos_substrate import LXDSubstrate
from .helpers import Bundle, cloud_type, get_kubeconfig, get_unit_cidrs, is_deployed

log = logging.getLogger(__name__)
TEST_DATA = Path(__file__).parent / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    --charm-file
        can be used multiple times, specifies which local charm files are available
    --upgrade-from
        instruct tests to start with a specific channel, and upgrade to these charms
    --snap-installation-resource
        path to the snap installation resource
    --lxd-containers
        if cloud is LXD, use containers
    --apply-proxy
        apply proxy to model-config

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--series", default=None, help="Series to deploy, overrides any markings")
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
    config.addinivalue_line(
        "markers",
        "bundle(file='', series='', apps_local={}, apps_channel={}, apps_resources={}): "
        "specify a YAML bundle file for a test.",
    )
    config.addinivalue_line(
        "markers",
        "clouds(*args): mark tests to run only on specific clouds.",
    )


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


async def cloud_proxied(ops_test: OpsTest):
    """Setup a cloud proxy settings if necessary

    If ghcr.io is reachable through a proxy apply expected proxy config to juju model.

    Args:
        ops_test (OpsTest): ops_test plugin
    """
    assert ops_test.model, "Model must be present"
    controller: juju.controller.Controller = await ops_test.model.get_controller()
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
    elif _type in ("ec2", "openstack") and ops_test.model:
        await ops_test.model.set_config({"container-networking-method": "local", "fan-config": ""})


@pytest.fixture(autouse=True)
async def skip_by_cloud_type(request, ops_test):
    """Skip tests based on cloud type."""
    if cloud_markers := request.node.get_closest_marker("clouds"):
        _type, _ = await cloud_type(ops_test)
        if _type not in cloud_markers.args:
            pytest.skip(f"cloud={_type} not among {cloud_markers.args}")


@contextlib.asynccontextmanager
async def deploy_model(
    ops_test: OpsTest,
    model_name: str,
    bundle: Bundle,
):
    """Add a juju model, deploy apps into it, wait for them to be active.

    Args:
        ops_test:          Instance of the pytest-operator plugin
        model_name:        name of the model in which to deploy
        bundle:            Bundle object to deploy or redeploy into the model

    Yields:
        model object
    """
    config: Optional[dict] = {}
    if ops_test.request.config.option.model_config:
        config = ops_test.read_model_config(ops_test.request.config.option.model_config)
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
            bundle_yaml = bundle.render(ops_test.tmp_path)
            await the_model.deploy(bundle_yaml, trust=bundle.needs_trust)
            await the_model.wait_for_idle(
                apps=list(bundle.applications),
                status="active",
                timeout=60 * 60,
            )
        try:
            yield the_model
        except GeneratorExit:
            log.fatal("Failed to determine model: model_name=%s", model_name)


@pytest_asyncio.fixture(scope="module")
async def kubernetes_cluster(request: pytest.FixtureRequest, ops_test: OpsTest):
    """Deploy kubernetes charms according to the bundle_marker."""
    model = "main"
    bundle, markings = await Bundle.create(ops_test)

    with ops_test.model_context(model) as the_model:
        if await is_deployed(the_model, bundle.path):
            log.info("Using existing model=%s.", the_model.uuid)
            yield ops_test.model
            return

    if request.config.option.no_deploy:
        pytest.skip("Skipping because of --no-deploy")

    log.info("Deploying new cluster using %s bundle.", bundle.path)
    if request.config.option.apply_proxy:
        await cloud_proxied(ops_test)

    await bundle.apply_marking(ops_test, markings)
    async with deploy_model(ops_test, model, bundle) as the_model:
        yield the_model


def valid_namespace_name(s: str) -> str:
    """Creates a valid kubernetes namespace name.

    Args:
        s: The string to sanitize.

    Returns:
        A valid namespace name.
    """
    valid_chars = set(string.ascii_lowercase + string.digits + "-")
    sanitized = "".join("-" if char not in valid_chars else char for char in s)
    sanitized = sanitized.strip("-")
    return sanitized[-63:]


@pytest_asyncio.fixture(scope="module")
async def api_client(
    kubernetes_cluster, ops_test: OpsTest, request  # pylint: disable=unused-argument
):
    """Create a k8s API client and namespace for the test.

    Args:
        kubernetes_cluster: The k8s model.
        ops_test: The pytest-operator plugin.
        request: The pytest request object.
    """
    module_name = request.module.__name__
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = valid_namespace_name(f"{module_name}-{rand_str}")
    kubeconfig_path = await get_kubeconfig(ops_test, module_name)
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))
    client = ApiClient(configuration=config)

    v1 = CoreV1Api(client)
    v1.create_namespace(
        body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
    )
    yield client
    v1.delete_namespace(name=namespace)


@pytest_asyncio.fixture(name="_grafana_agent", scope="module")
async def grafana_agent(kubernetes_cluster: Model):
    """Deploy Grafana Agent."""
    primary = kubernetes_cluster.applications["k8s"]
    data = primary.units[0].machine.safe_data
    arch = data["hardware-characteristics"]["arch"]
    series = juju.utils.get_version_series(data["base"].split("@")[1])
    url = URL("ch", name="grafana-agent", series=series, architecture=arch)

    await kubernetes_cluster.deploy(url, channel="stable", series=series)
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
