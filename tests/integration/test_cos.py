#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import contextlib
import json
import logging
import secrets
from pathlib import Path
from typing import Iterator, Optional

import jubilant
import k8s_cloud
import pytest
from cloud import cloud_type, model_owner
from cos_substrate import COSSubstrate
from grafana import Grafana
from helpers import stage, wait_active
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from literals import REPO_ROOT, TEST_DATA, VERSION_SERIES
from lxd_substrate import VMOptions
from prometheus import Prometheus
from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)

# NOTE: (hue) Skipping entire module since the infra and test is flaky
pytest.skip("Skipping: Infra and tests are flaky, skipping.", allow_module_level=True)


APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test-bundle-cos.yaml", apps_local=APPS),
    pytest.mark.architecture("amd64"),
]

METRICS_AGENTS = ["grafana-agent:1/stable", "opentelemetry-collector:2/edge"]
COS_CHARMS = ["alertmanager", "catalogue", "grafana", "loki", "prometheus", "traefik"]
ONE_HOUR = 60 * 60


@contextlib.contextmanager
def _log_cli_errors(what: str) -> Iterator[None]:
    """Swallow a juju CLI failure during teardown, but say so in the log.

    Args:
        what: Human-readable description of the operation, used in the log message.

    Yields:
        None.
    """
    try:
        yield
    except jubilant.CLIError:
        log.exception("Teardown step failed and was ignored: %s", what)


@pytest.fixture(scope="module", params=METRICS_AGENTS)
def metrics_agent(
    k8s_cluster: jubilant.Juju, request: pytest.FixtureRequest, timeout: int
) -> Iterator[str]:
    """Deploy the metrics agent charm and integrate it with the cluster.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        request: Pytest fixture request.
        timeout: Timeout in minutes.

    Yields:
        The name of the metrics agent application.
    """
    option = request.config.option.metrics_agent_charm
    if option and option not in request.param:
        pytest.skip(
            f"Skipping metrics agent charm {request.param} due to --metrics-agent-charm={option}"
        )

    agent, agent_channel = request.param.split(":")
    status = k8s_cluster.status()
    assert "k8s" in status.apps, "k8s application not found in the model"
    has_worker = "k8s-worker" in status.apps

    base = status.apps["k8s"].base
    assert base, "Could not determine the base of the k8s application"
    k8s_unit = next(iter(status.get_units("k8s").values()))
    arch = dict(
        pair.split("=", 1)
        for pair in (status.machines[k8s_unit.machine].hardware or "").split()
        if "=" in pair
    )["arch"]
    log.info("Deploying %s on ubuntu@%s/%s", agent, base.channel, arch)
    assert VERSION_SERIES.get(base.channel), f"Unknown base channel {base.channel}"

    k8s_cluster.deploy(
        agent,
        channel=agent_channel,
        base=f"{base.name}@{base.channel}",
        constraints={"arch": arch},
    )
    k8s_cluster.integrate(f"{agent}:cos-agent", "k8s:cos-agent")
    if has_worker:
        k8s_cluster.integrate(f"{agent}:cos-agent", "k8s-worker:cos-agent")
        k8s_cluster.integrate("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")

    yield agent

    k8s_cluster.remove_application(agent)
    if has_worker:
        k8s_cluster.remove_relation("k8s:cos-worker-tokens", "k8s-worker:cos-tokens")
        k8s_cluster.wait(
            lambda s: "cos-worker-tokens" not in s.apps["k8s"].relations,
            timeout=timeout * 60,
        )


@pytest.fixture(scope="module")
def cos_substrate(
    k8s_cluster: jubilant.Juju,
    metrics_agent: str,  # noqa: ARG001 -- ordering only, as in the pytest-operator suite
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Create a COS substrate and yield a kubeconfig pointing at it.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        metrics_agent: Ensures the metrics agent outlives the COS substrate.
        request: Pytest fixture request.
        tmp_path_factory: Pytest temporary path factory.

    Yields:
        Path to the kubeconfig of the COS substrate.
    """
    provider, vms = cloud_type(k8s_cluster.model, request.config.option.lxd_containers)
    assert provider == "lxd", "COS tests only supported on LXD clouds"
    manager: Optional[COSSubstrate] = None
    config: Optional[bytes] = None
    try:
        manager = COSSubstrate(VMOptions() if vms else None)
        config = manager.create_substrate()
        kubeconfig_path = tmp_path_factory.mktemp("cos") / "kubeconfig"
        kubeconfig_path.write_bytes(config)
        yield kubeconfig_path
    finally:
        if config and manager:
            manager.teardown_substrate()


@pytest.fixture(scope="module")
def cos_model(k8s_cluster: jubilant.Juju, cos_substrate: Path) -> Iterator[jubilant.Juju]:
    """Register the COS substrate as a Juju cloud and create a model on it.

    The COS model is created and destroyed here rather than through pytest-jubilant's
    ``juju_factory``, for two reasons:

    * ``juju_factory`` is set up before this fixture (it backs the ``juju`` fixture), so
      its finalizer runs *last* -- after ``cos_substrate`` has already destroyed the
      cluster the model lives on. Its unconditional ``juju debug-log`` would then fail
      against an unreachable model, and that exception would skip the teardown of every
      other model it owns.
    * ``juju_factory`` honours ``--no-juju-teardown``, which the ``--keep-models`` flag
      that CI always passes maps onto. The pytest-operator suite deliberately used
      ``ModelKeep.NEVER`` here, so the COS model was always destroyed.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        cos_substrate: Path to the COS substrate's kubeconfig.

    Yields:
        A Jubilant Juju instance bound to the COS model.
    """
    controller = k8s_cluster.show_model().controller_name
    cloud_name = f"k8s-cloud-{secrets.token_hex(3)}"
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(cos_substrate))

    cos = jubilant.Juju()
    try:
        k8s_cloud.add_k8s(
            k8s_cluster, cloud_name, config, controller=controller, skip_storage=False
        )
        cos.add_model(f"cos-{secrets.token_hex(3)}", cloud=cloud_name, controller=controller)
        yield cos
    finally:
        # Destroy the model before removing the cloud it lives on, and before
        # cos_substrate tears the underlying cluster down.
        if cos.model:
            try:
                cos.destroy_model(cos.model, destroy_storage=True, force=True, timeout=ONE_HOUR)
            except jubilant.CLIError:
                log.exception("Failed to destroy the COS model %s", cos.model)
        k8s_cloud.remove_k8s(k8s_cluster, cloud_name, controller=controller)


@pytest.fixture(name="_cos_lite_installed", scope="module")
def cos_lite_installed(cos_model: jubilant.Juju) -> Iterator[None]:
    """Install the COS Lite bundle into the COS model.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.

    Yields:
        None.
    """
    log.info("Deploying COS bundle ...")
    overlay = stage(TEST_DATA / "cos-offers-overlay.yaml", __name__)
    cos_model.deploy("cos-lite", channel="edge", trust=True, overlays=[overlay])
    cos_model.wait(
        lambda status: all(charm in status.apps for charm in COS_CHARMS), timeout=ONE_HOUR
    )
    # The pytest-operator suite used raise_on_error=False here: COS Lite charms error
    # transiently while settling, and aborting on the first one made the fixture flaky.
    wait_active(cos_model, timeout=ONE_HOUR, raise_on_error=False)

    yield

    log.info("Removing COS Lite charms...")
    cos_model.remove_application(*COS_CHARMS, destroy_storage=True, force=True)
    cos_model.wait(
        lambda status: all(charm not in status.apps for charm in COS_CHARMS), timeout=ONE_HOUR
    )


@pytest.fixture(scope="module")
def traefik_url(cos_model: jubilant.Juju, _cos_lite_installed) -> str:
    """Fetch the Traefik proxied endpoint URL.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        _cos_lite_installed: Ensures the COS Lite bundle is deployed.

    Returns:
        The Traefik base URL.
    """
    task = cos_model.run("traefik/0", "show-proxied-endpoints")
    endpoints = json.loads(task.results["proxied-endpoints"])
    return endpoints["traefik"]["url"]


@pytest.fixture(scope="module")
def expected_dashboard_titles() -> set:
    """Read the expected Grafana dashboard titles from the charm source.

    Returns:
        Set of dashboard titles.
    """
    grafana_dir = REPO_ROOT / "charms/worker/k8s/src/grafana_dashboards"
    titles = set()
    for path in grafana_dir.iterdir():
        if path.is_file() and path.name.endswith(".json"):
            titles.add(json.loads(path.read_text())["title"])
    return titles


def _offer_url(cos_model: jubilant.Juju, offer: str) -> str:
    """Build the cross-model offer URL for an offer in the COS model.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        offer: Offer name.

    Returns:
        The ``<owner>/<model>.<offer>`` URL.
    """
    info = cos_model.show_model()
    return f"{model_owner(cos_model)}/{info.short_name}.{offer}"


@pytest.fixture(name="_related_grafana", scope="module")
def related_grafana(
    k8s_cluster: jubilant.Juju, cos_model: jubilant.Juju, metrics_agent: str, _cos_lite_installed
) -> Iterator[None]:
    """Integrate the metrics agent with Grafana across models.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        cos_model: Jubilant Juju instance bound to the COS model.
        metrics_agent: Name of the metrics agent application.
        _cos_lite_installed: Ensures the COS Lite bundle is deployed.

    Yields:
        None.
    """
    log.info("Integrating with Grafana")
    k8s_cluster.integrate(metrics_agent, _offer_url(cos_model, "grafana-dashboards"))
    wait_active(cos_model, timeout=ONE_HOUR)
    wait_active(k8s_cluster, timeout=ONE_HOUR)

    yield

    log.info("Removing Grafana SAAS ...")
    with _log_cli_errors("remove grafana-dashboards SAAS"):
        k8s_cluster.cli("remove-saas", "grafana-dashboards")
    log.info("Removing Grafana Offer...")
    with _log_cli_errors("remove grafana-dashboards offer"):
        cos_model.cli(
            "remove-offer",
            f"{cos_model.show_model().short_name}.grafana-dashboards",
            "--force",
            "-y",
            include_model=False,
        )


@pytest.fixture(scope="module")
def grafana_password(cos_model: jubilant.Juju, _related_grafana) -> str:
    """Fetch the Grafana admin password.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        _related_grafana: Ensures Grafana is integrated.

    Returns:
        The Grafana admin password.
    """
    return cos_model.run("grafana/0", "get-admin-password").results["admin-password"]


@pytest.fixture(scope="module")
def related_prometheus(
    k8s_cluster: jubilant.Juju, cos_model: jubilant.Juju, metrics_agent: str, _cos_lite_installed
) -> Iterator[None]:
    """Integrate the metrics agent with Prometheus across models.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        cos_model: Jubilant Juju instance bound to the COS model.
        metrics_agent: Name of the metrics agent application.
        _cos_lite_installed: Ensures the COS Lite bundle is deployed.

    Yields:
        None.
    """
    log.info("Integrating with Prometheus")
    k8s_cluster.integrate(metrics_agent, _offer_url(cos_model, "prometheus-receive-remote-write"))
    wait_active(k8s_cluster, timeout=ONE_HOUR)
    wait_active(cos_model, timeout=ONE_HOUR)

    yield

    log.info("Removing Prometheus Remote Write SAAS ...")
    with _log_cli_errors("remove prometheus-receive-remote-write SAAS"):
        k8s_cluster.cli("remove-saas", "prometheus-receive-remote-write")
    log.info("Removing Prometheus Offer...")
    with _log_cli_errors("remove prometheus-receive-remote-write offer"):
        cos_model.cli(
            "remove-offer",
            f"{cos_model.show_model().short_name}.prometheus-receive-remote-write",
            "--force",
            "-y",
            include_model=False,
        )


@pytest.mark.cos
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: jubilant.Juju,
):
    """Test integration with Grafana."""
    grafana = Grafana(
        model_name=cos_model.show_model().short_name,
        base=traefik_url,
        password=grafana_password,
    )
    assert grafana.is_ready(), "Grafana is not ready"
    actual_dashboard_titles = {dashboard.get("title") for dashboard in grafana.dashboards_all()}
    assert expected_dashboard_titles.issubset(actual_dashboard_titles)


@pytest.mark.cos
@pytest.mark.usefixtures("related_prometheus")
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
def test_prometheus(traefik_url: str, cos_model: jubilant.Juju):
    """Test integration with Prometheus."""
    prometheus = Prometheus(model_name=cos_model.show_model().short_name, base=traefik_url)
    assert prometheus.is_ready(), "Prometheus is not ready"

    queries = [
        'up{job="etcd"} > 0',
        'up{job="kubelet", metrics_path="/metrics"} > 0',
        'up{job="kubelet", metrics_path="/metrics/cadvisor"} > 0',
        'up{job="kubelet", metrics_path="/metrics/probes"} > 0',
        'up{job="apiserver"} > 0',
        'up{job="kube-controller-manager"} > 0',
        'up{job="kube-scheduler"} > 0',
        'up{job="kube-proxy"} > 0',
        'up{job="kube-state-metrics"} > 0',
    ]
    failed = [query for query in queries if not prometheus.get_metrics(query)]
    assert not failed, f"Failed queries: {failed}"
