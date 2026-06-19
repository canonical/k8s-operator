# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for charm tests."""

import contextlib
import json
import logging
import os
import random
import secrets
import string
from pathlib import Path
from typing import Generator, Optional

import jubilant
import kubernetes.client.models as k8s_models
import pytest
import yaml
from cos_substrate import COSSubstrate
from helpers import (
    Bundle,
    cloud_type,
    fast_forward,
    get_kubeconfig,
    get_unit_cidrs,
)
from kubernetes import config as k8s_config
from kubernetes.client import ApiClient, Configuration, CoreV1Api
from literals import ONE_MIN
from lxd_substrate import LXDSubstrate, VMOptions

log = logging.getLogger(__name__)
TEST_DATA = Path(__file__).parent / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"
METRICS_AGENTS = ["opentelemetry-collector:2/stable"]


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
            "skipping all others (e.g., opentelemetry-collector:2/stable)."
        ),
    )
    # Options previously provided by pytest-operator that the code and CI rely on.
    parser.addoption(
        "--model", default=None, help="Use an existing model instead of a temporary one"
    )
    parser.addoption(
        "--keep-models",
        action="store_true",
        default=False,
        help="Keep temporarily-created models",
    )
    parser.addoption(
        "--no-deploy",
        action="store_true",
        default=False,
        help="Reuse an already-deployed model; skip deployment",
    )
    parser.addoption("--model-config", default=None, help="Path to a model-config YAML")
    parser.addoption("--cloud", default=None, help="Cloud (and optional region) to deploy to")
    parser.addoption("--controller", default=None, help="Juju controller to use")


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


def _read_model_config(request) -> Optional[dict]:
    """Read the model config YAML pointed to by --model-config, if any.

    Args:
        request: pytest request object

    Returns:
        Parsed model config mapping or None.
    """
    path = request.config.option.model_config
    if not path:
        return None
    return yaml.safe_load(Path(path).read_text())


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> Generator[jubilant.Juju, None, None]:
    """Module-scoped Juju instance against a temporary (or existing) model."""
    timeout_min = max(60, request.config.option.timeout)
    existing = request.config.getoption("--model")

    if existing:
        the_juju = jubilant.Juju(model=existing)
        the_juju.wait_timeout = timeout_min * 60
        yield the_juju
        if request.session.testsfailed:
            log.info("Model debug-log:\n%s", the_juju.debug_log(limit=1000))
        return

    keep = request.config.getoption("--keep-models")
    controller = request.config.getoption("--controller")
    cloud = request.config.getoption("--cloud")
    config = _read_model_config(request)
    with jubilant.temp_model(
        keep=keep, controller=controller, cloud=cloud, config=config
    ) as the_juju:
        the_juju.wait_timeout = timeout_min * 60
        yield the_juju
        if request.session.testsfailed:
            log.info("Model debug-log:\n%s", the_juju.debug_log(limit=1000))


@pytest.fixture(scope="module")
def module_tmp_path(tmp_path_factory, request) -> Path:
    """Module-scoped temporary directory."""
    return tmp_path_factory.mktemp(request.module.__name__.replace(".", "_"))


def cloud_proxied(juju: jubilant.Juju):
    """Set up cloud proxy settings if necessary.

    If ghcr.io is reachable through a proxy apply expected proxy config to juju model.

    Args:
        juju: jubilant Juju instance
    """
    ctrl_prefix = ""
    if juju.model and ":" in juju.model:
        ctrl_prefix = juju.model.split(":", 1)[0] + ":"
    controller_model = jubilant.Juju(model=f"{ctrl_prefix}controller")
    proxy_config_file = TEST_DATA / "static-proxy-config.yaml"
    proxy_configs = yaml.safe_load(proxy_config_file.read_text())
    local_no_proxy = get_unit_cidrs(controller_model, "controller", 0)
    no_proxy = {*proxy_configs["juju-no-proxy"], *local_no_proxy}
    proxy_configs["juju-no-proxy"] = ",".join(sorted(no_proxy))
    juju.model_config(proxy_configs)


def cloud_profile(request, juju: jubilant.Juju):
    """Apply Cloud Specific Settings to the model.

    Args:
        request: pytest request object
        juju: jubilant Juju instance
    """
    _type, _vms = cloud_type(juju, request.config.getoption("--lxd-containers"))
    if _type == "lxd":
        # lxd-profile to the model if the juju cloud is lxd.
        lxd = LXDSubstrate()

        lxd_profiles, lxd_networks = [], []
        # -- Setup LXD networks and profiles for the model.
        cloud_mark = request.node.get_closest_marker("clouds")
        if cloud_mark and "lxd" in cloud_mark.args:
            if networks := cloud_mark.kwargs.get("networks"):
                lxd_networks.extend(networks)
            if profiles := cloud_mark.kwargs.get("profiles"):
                lxd_profiles.extend(profiles)

        model_uuid = juju.show_model().model_uuid
        # juju names the model's lxd profile "juju-<model>-<uuid prefix>" using the bare
        # model name (without any "<controller>:" prefix).
        model_name = (juju.model or "").rpartition(":")[-1]
        profile_name = f"juju-{model_name}-{model_uuid[:6]}"
        lxd.configure_networks(lxd_networks)
        lxd.remove_profile(profile_name)
        lxd.apply_profile(lxd_profiles, profile_name)

    elif _type in ("ec2", "openstack"):
        juju.model_config({"container-networking-method": "local", "fan-config": ""})


@pytest.fixture(scope="module", autouse=True)
def skip_by_cloud_type(request, juju: jubilant.Juju):
    """Skip tests based on cloud type."""
    if cloud_markers := request.node.get_closest_marker("clouds"):
        _type, _ = cloud_type(juju, request.config.getoption("--lxd-containers"))
        if _type not in cloud_markers.args:
            pytest.skip(f"cloud={_type} not among {cloud_markers.args}")


@contextlib.contextmanager
def deploy_model(
    request,
    juju: jubilant.Juju,
    bundle: Bundle,
    tmp_path: Path,
):
    """Deploy apps into the model and wait for them to be active.

    Args:
        request:    pytest request object
        juju:       jubilant Juju instance
        bundle:     Bundle object to deploy or redeploy into the model
        tmp_path:   path to render the bundle into

    Yields:
        the juju instance
    """
    at_least_60 = max(60, request.config.option.timeout)
    cloud_profile(request, juju)
    with fast_forward(juju, ONE_MIN):
        bundle_path = bundle.render(tmp_path)
        juju.deploy(bundle_path, trust=bundle.needs_trust)
        juju.wait(
            lambda status: jubilant.all_active(status, *bundle.applications),
            timeout=at_least_60 * 60,
        )
    yield juju


@pytest.fixture(scope="module")
def kubernetes_cluster(
    request: pytest.FixtureRequest, juju: jubilant.Juju, module_tmp_path: Path
) -> Generator[jubilant.Juju, None, None]:
    """Deploy kubernetes charms according to the bundle_marker."""
    bundle, markings = Bundle.create(request, juju)

    if bundle.is_deployed(juju):
        log.info("Using existing model=%s.", juju.model)
        yield juju
        return

    if request.config.option.no_deploy:
        pytest.skip("Skipping because of --no-deploy")

    log.info("Deploying new cluster using %s bundle.", bundle.path)
    if request.config.option.apply_proxy:
        cloud_proxied(juju)

    bundle.apply_marking(request, juju, markings)
    with deploy_model(request, juju, bundle, module_tmp_path):
        yield juju


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
    kubernetes_cluster: jubilant.Juju,
    module_tmp_path: Path,
    request,
):
    """Create a k8s API client and namespace for the test.

    Args:
        kubernetes_cluster: The k8s model juju instance.
        module_tmp_path: Module-scoped temporary directory.
        request: The pytest request object.
    """
    module_name = request.module.__name__
    rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    namespace = valid_namespace_name(f"{module_name}-{rand_str}")
    kubeconfig_path = get_kubeconfig(kubernetes_cluster, module_tmp_path, module_name)
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig_path))
    client = ApiClient(configuration=config)

    v1 = CoreV1Api(client)
    v1.create_namespace(
        body=k8s_models.V1Namespace(metadata=k8s_models.V1ObjectMeta(name=namespace))
    )
    yield client
    v1.delete_namespace(name=namespace)


def _machine_arch(hardware: str) -> str:
    """Extract the architecture from a machine ``hardware`` string.

    Args:
        hardware: machine hardware string, e.g. "arch=amd64 cores=2 mem=4096M".

    Returns:
        the architecture, e.g. "amd64".
    """
    for kv in hardware.split():
        key, _, value = kv.partition("=")
        if key == "arch":
            return value
    return ""


@pytest.fixture(scope="module", params=METRICS_AGENTS)
def metrics_agent(kubernetes_cluster: jubilant.Juju, request):
    """Deploy Metrics Agent Charm."""
    option = request.config.option.metrics_agent_charm
    if option and option not in request.param:
        pytest.skip(
            f"Skipping metrics agent charm {request.param} due to --metrics-agent-charm={option}"
        )

    metrics_agent, metrics_agent_channel = request.param.split(":")
    status = kubernetes_cluster.status()
    if "k8s" not in status.apps:
        pytest.fail("k8s application not found in the model")
    worker = "k8s-worker" in status.apps

    first_unit = next(iter(status.get_units("k8s").values()))
    machine = status.machines[first_unit.machine]
    arch = _machine_arch(machine.hardware)
    assert machine.base, "k8s machine base is unknown"
    base = f"{machine.base.name}@{machine.base.channel}"

    kubernetes_cluster.deploy(
        metrics_agent, channel=metrics_agent_channel, base=base, constraints={"arch": arch}
    )
    kubernetes_cluster.integrate(f"{metrics_agent}:cos-agent", "k8s:cos-agent")
    if worker:
        kubernetes_cluster.integrate(f"{metrics_agent}:cos-agent", "k8s-worker:cos-agent")
        kubernetes_cluster.integrate("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")

    yield metrics_agent

    kubernetes_cluster.remove_application(metrics_agent)
    if worker:
        kubernetes_cluster.remove_relation("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")


@pytest.fixture(scope="module")
def cos_substrate(
    request, kubernetes_cluster: jubilant.Juju, metrics_agent, module_tmp_path: Path
):
    """Create a COS substrate and yield a kubeconfig to it."""
    _type, _vms = cloud_type(kubernetes_cluster, request.config.getoption("--lxd-containers"))
    assert _type == "lxd", "COS tests only supported on LXD clouds"
    manager: Optional[COSSubstrate] = None
    config: Optional[bytes] = None
    try:
        manager = COSSubstrate(VMOptions() if _vms else None)
        config = manager.create_substrate()
        kubeconfig_path = module_tmp_path / "kubeconfig"
        kubeconfig_path.write_bytes(config)
        yield kubeconfig_path
    finally:
        if config and manager:
            manager.teardown_substrate()


def _add_k8s_cloud(juju: jubilant.Juju, cloud_name: str, controller: str, kubeconfig: Path):
    """Register a Kubernetes cloud from a kubeconfig with the client and controller.

    Args:
        juju: jubilant Juju instance
        cloud_name: name to register the cloud under
        controller: controller to add the cloud to
        kubeconfig: path to the kubeconfig file
    """
    old = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = str(kubeconfig)
    try:
        juju.cli(
            "add-k8s",
            cloud_name,
            "--client",
            "--controller",
            controller,
            include_model=False,
        )
    finally:
        if old is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = old


@pytest.fixture(scope="module")
def cos_model(request, kubernetes_cluster: jubilant.Juju, cos_substrate: Path):
    """Create a Juju model into which COS can be deployed."""
    controller = kubernetes_cluster.status().model.controller
    cloud_name = "cos-k8s-" + secrets.token_hex(3)
    cos = jubilant.Juju()
    cos.wait_timeout = max(60, request.config.option.timeout) * 60
    try:
        _add_k8s_cloud(cos, cloud_name, controller, cos_substrate)
        cos.add_model("cos", cloud=cloud_name)
        yield cos
    finally:
        with contextlib.suppress(Exception):
            cos.destroy_model("cos", destroy_storage=True, force=True, timeout=10 * 60)
        with contextlib.suppress(Exception):
            cos.cli(
                "remove-k8s",
                cloud_name,
                "--client",
                "--controller",
                controller,
                include_model=False,
            )


@pytest.fixture(name="_cos_lite_installed", scope="module")
def cos_lite_installed(request, cos_model: jubilant.Juju):
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
    overlay = TEST_DATA / "cos-offers-overlay.yaml"
    cos_model.deploy("cos-lite", channel="edge", trust=True, overlays=[overlay])
    cos_model.wait(
        lambda status: (
            all(app in status.apps for app in cos_charms)
            and jubilant.all_active(status, *cos_charms)
        ),
        timeout=20 * 60,
    )

    yield
    log.info("Removing COS Lite charms...")
    for charm in cos_charms:
        log.info("Removing %s...", charm)
        cos_model.remove_application(charm, destroy_storage=True, force=True)
    cos_model.wait(
        lambda status: all(app not in status.apps for app in cos_charms),
        timeout=60 * 10,
    )


@pytest.fixture(scope="module")
def traefik_url(cos_model: jubilant.Juju, _cos_lite_installed):
    """Fixture to fetch Traefik url."""
    task = cos_model.run("traefik/0", "show-proxied-endpoints")
    p_e = json.loads(task.results["proxied-endpoints"])

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


def _model_owner(juju: jubilant.Juju) -> str:
    """Return the username that owns the current model (the offer URL qualifier).

    Args:
        juju: jubilant Juju instance

    Returns:
        the current user name.
    """
    return json.loads(juju.cli("whoami", "--format", "json", include_model=False))["user"]


@pytest.fixture(name="_related_grafana", scope="module")
def related_grafana(
    kubernetes_cluster: jubilant.Juju, cos_model: jubilant.Juju, metrics_agent, _cos_lite_installed
):
    """Fixture to integrate with Grafana."""
    model_owner = _model_owner(cos_model)
    cos_model_name = cos_model.model

    log.info("Integrating with Grafana")
    kubernetes_cluster.integrate(
        metrics_agent,
        f"{model_owner}/{cos_model_name}.grafana-dashboards",
    )
    cos_model.wait(jubilant.all_active)
    kubernetes_cluster.wait(jubilant.all_active)

    yield

    log.info("Removing Grafana SAAS ...")
    kubernetes_cluster.cli("remove-saas", "grafana-dashboards")
    log.info("Removing Grafana Offer...")
    cos_model.cli(
        "remove-offer",
        f"{cos_model_name}.grafana-dashboards",
        "--force",
        "-y",
        include_model=False,
    )


@pytest.fixture(scope="module")
def grafana_password(cos_model: jubilant.Juju, _related_grafana):
    """Fixture to get Grafana password."""
    task = cos_model.run("grafana/0", "get-admin-password")
    yield task.results["admin-password"]


@pytest.fixture(scope="module")
def related_prometheus(
    kubernetes_cluster: jubilant.Juju, cos_model: jubilant.Juju, metrics_agent, _cos_lite_installed
):
    """Fixture to integrate with Prometheus."""
    model_owner = _model_owner(cos_model)
    cos_model_name = cos_model.model

    log.info("Integrating with Prometheus")
    kubernetes_cluster.integrate(
        metrics_agent,
        f"{model_owner}/{cos_model_name}.prometheus-receive-remote-write",
    )
    kubernetes_cluster.wait(jubilant.all_active)
    cos_model.wait(jubilant.all_active)

    yield

    log.info("Removing Prometheus Remote Write SAAS ...")
    kubernetes_cluster.cli("remove-saas", "prometheus-receive-remote-write")
    log.info("Removing Prometheus Offer...")
    cos_model.cli(
        "remove-offer",
        f"{cos_model_name}.prometheus-receive-remote-write",
        "--force",
        "-y",
        include_model=False,
    )


@pytest.fixture(scope="module")
def timeout(request):
    """Fixture to set the timeout for certain tests."""
    return request.config.option.timeout
