#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the COS integration.

The cluster under test is observed from the outside: a second, pinned Canonical K8s
cluster (``cos-host``) is deployed next to it in the same model, registered as a Juju
cloud, and hosts a small COS Lite stack. The ``opentelemetry-collector`` charm on the
cluster under test is then related to that stack across models, and the tests check
that the metrics and dashboards the k8s charm advertises show up in Prometheus and
Grafana.
"""

import contextlib
import json
import logging
import secrets
from pathlib import Path
from typing import Callable, Iterator

import jubilant
import k8s_cloud
import pytest
from cloud import model_owner
from grafana import Grafana
from helpers import get_kubeconfig, stage, wait_active
from kubernetes import config as k8s_config
from kubernetes.client import Configuration
from literals import REPO_ROOT, TEST_DATA
from prometheus import Prometheus
from tenacity import Retrying, stop_after_delay, wait_fixed

log = logging.getLogger(__name__)

APPS = ["k8s"]
pytestmark = [
    pytest.mark.bundle(file="test-bundle-cos.yaml", apps_local=APPS),
    pytest.mark.architecture("amd64"),
]

COLLECTOR = "opentelemetry-collector"
COLLECTOR_CHANNEL = "0.130/stable"
COS_HOST = "cos-host"
COS_APPS = ["traefik", "prometheus", "grafana"]
OFFERS = ["grafana-dashboards", "prometheus-receive-remote-write"]

# Scraping, remote-writing and ingress propagation are asynchronous; poll rather than
# sleep, and fail with the specifics of what is still missing.
POLL_INTERVAL = 10
POLL_DEADLINE = 15 * 60


def _poll(check: Callable[[], None]) -> None:
    """Run ``check`` until it stops raising or the deadline passes.

    Args:
        check: Callable that raises while the expectation does not hold yet.
    """
    for attempt in Retrying(
        stop=stop_after_delay(POLL_DEADLINE), wait=wait_fixed(POLL_INTERVAL), reraise=True
    ):
        with attempt:
            check()


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


@pytest.fixture(scope="module")
def collector(k8s_cluster: jubilant.Juju) -> Iterator[str]:
    """Deploy the metrics collector charm and integrate it with the cluster under test.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.

    Yields:
        The name of the collector application.
    """
    base = k8s_cluster.status().apps["k8s"].base
    assert base, "Could not determine the base of the k8s application"
    log.info("Deploying %s on ubuntu@%s", COLLECTOR, base.channel)

    k8s_cluster.deploy(COLLECTOR, channel=COLLECTOR_CHANNEL, base=f"{base.name}@{base.channel}")
    k8s_cluster.integrate(f"{COLLECTOR}:cos-agent", "k8s:cos-agent")

    yield COLLECTOR

    k8s_cluster.remove_application(COLLECTOR)


@pytest.fixture(scope="module")
def cos_ingress_ip(k8s_cluster: jubilant.Juju, timeout: int) -> str:
    """Make the COS cluster's own address the load-balancer address Traefik is served on.

    Everything that talks to COS (the collector next to the cluster under test, and this
    test run) already reaches the COS cluster's node address, so using it as the
    load-balancer address needs no address pool of its own.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        timeout: Timeout in minutes.

    Returns:
        The address Traefik will be reachable on.
    """
    unit = next(iter(k8s_cluster.status().get_units(COS_HOST).values()))
    address = unit.public_address
    assert address, f"{COS_HOST} unit has no address"
    log.info("Serving COS load balancers from %s/32", address)

    k8s_cluster.config(
        COS_HOST, {"load-balancer-enabled": True, "load-balancer-cidrs": f"{address}/32"}
    )
    wait_active(k8s_cluster, COS_HOST, timeout=timeout * 60)
    return address


@pytest.fixture(scope="module")
def cos_model(
    k8s_cluster: jubilant.Juju,
    cos_ingress_ip: str,  # noqa: ARG001 -- ordering only: COS must start with a load balancer
    tmp_path_factory: pytest.TempPathFactory,
    timeout: int,
) -> Iterator[jubilant.Juju]:
    """Register the COS cluster as a Juju cloud and create a model on it.

    The COS model is created and destroyed here rather than through pytest-jubilant's
    ``juju_factory``: ``juju_factory`` honours ``--no-juju-teardown``, which the
    ``--keep-models`` flag that CI always passes maps onto, and the COS model must always
    be destroyed. It also has to go before the test model, which holds the cluster it
    lives on.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        cos_ingress_ip: Ensures the COS cluster has its load balancer configured.
        tmp_path_factory: Pytest temporary path factory.
        timeout: Timeout in minutes.

    Yields:
        A Jubilant Juju instance bound to the COS model.
    """
    kubeconfig: Path = get_kubeconfig(k8s_cluster, tmp_path_factory.mktemp(COS_HOST), app=COS_HOST)
    controller = k8s_cluster.show_model().controller_name
    cloud_name = f"k8s-cloud-{secrets.token_hex(3)}"
    config = type.__call__(Configuration)
    k8s_config.load_config(client_configuration=config, config_file=str(kubeconfig))

    cos = jubilant.Juju()
    try:
        k8s_cloud.add_k8s(
            k8s_cluster, cloud_name, config, controller=controller, skip_storage=False
        )
        cos.add_model(f"cos-{secrets.token_hex(3)}", cloud=cloud_name, controller=controller)
        yield cos
    finally:
        # Destroy the model before removing the cloud it lives on.
        if cos.model:
            try:
                cos.destroy_model(
                    cos.model, destroy_storage=True, force=True, timeout=timeout * 60
                )
            except jubilant.CLIError:
                log.exception("Failed to destroy the COS model %s", cos.model)
        k8s_cloud.remove_k8s(k8s_cluster, cloud_name, controller=controller)


@pytest.fixture(scope="module")
def cos_stack(cos_model: jubilant.Juju, timeout: int) -> None:
    """Deploy the pinned COS Lite stack into the COS model.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        timeout: Timeout in minutes.
    """
    log.info("Deploying COS bundle ...")
    cos_model.deploy(stage(TEST_DATA / "cos-bundle.yaml", __name__), trust=True)
    # COS charms error transiently while settling; aborting on the first one made this
    # wait flaky, so let them recover and only fail if they never do.
    wait_active(cos_model, *COS_APPS, timeout=timeout * 60, raise_on_error=False)


@pytest.fixture(scope="module")
def traefik_url(cos_model: jubilant.Juju, cos_stack: None) -> str:
    """Fetch the Traefik proxied endpoint URL.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        cos_stack: Ensures the COS stack is deployed.

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


@pytest.fixture(scope="module")
def cos_integrated(
    k8s_cluster: jubilant.Juju,
    cos_model: jubilant.Juju,
    collector: str,
    cos_stack: None,
    traefik_url: str,
    timeout: int,
) -> Iterator[None]:
    """Integrate the collector with Grafana and Prometheus across models.

    Args:
        k8s_cluster: Jubilant Juju instance with the cluster deployed.
        cos_model: Jubilant Juju instance bound to the COS model.
        collector: Name of the collector application.
        cos_stack: Ensures the COS stack is deployed.
        traefik_url: Traefik base URL.
        timeout: Timeout in minutes.

    Yields:
        None.
    """
    log.info("Integrating with Grafana and Prometheus")
    for offer in OFFERS:
        k8s_cluster.integrate(collector, _offer_url(cos_model, offer))
    wait_active(k8s_cluster, timeout=timeout * 60)
    wait_active(cos_model, *COS_APPS, timeout=timeout * 60, raise_on_error=False)

    # The collector remote-writes through Traefik from inside the cluster under test.
    # Check that path on its own so a network problem is not reported as missing metrics.
    ready = f"{Prometheus(cos_model.show_model().short_name, traefik_url).base_uri}/-/ready"

    def reachable() -> None:
        try:
            k8s_cluster.exec(
                "curl", "--silent", "--show-error", "--fail", "--max-time", "10", ready,
                unit="k8s/0",
            )  # fmt: skip
        except jubilant.TaskError as exc:
            raise AssertionError(
                f"k8s/0 cannot reach Prometheus through Traefik at {ready}: "
                f"{exc.task.stderr.strip() or exc.task.stdout.strip()}"
            ) from exc

    _poll(reachable)

    yield

    for offer in OFFERS:
        with _log_cli_errors(f"remove {offer} SAAS"):
            k8s_cluster.cli("remove-saas", offer)


@pytest.fixture(scope="module")
def grafana_password(cos_model: jubilant.Juju, cos_stack: None) -> str:
    """Fetch the Grafana admin password.

    Args:
        cos_model: Jubilant Juju instance bound to the COS model.
        cos_stack: Ensures the COS stack is deployed.

    Returns:
        The Grafana admin password.
    """
    return cos_model.run("grafana/0", "get-admin-password").results["admin-password"]


@pytest.mark.cos
@pytest.mark.usefixtures("cos_integrated")
def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: jubilant.Juju,
):
    """Test that the charm's dashboards reach Grafana."""
    grafana = Grafana(
        model_name=cos_model.show_model().short_name,
        base=traefik_url,
        password=grafana_password,
    )

    def check() -> None:
        assert grafana.is_ready(), "Grafana is not ready"
        actual = {dashboard.get("title") for dashboard in grafana.dashboards_all()}
        missing = expected_dashboard_titles - actual
        assert not missing, f"Dashboards missing from Grafana: {sorted(missing)}"

    _poll(check)


@pytest.mark.cos
@pytest.mark.usefixtures("cos_integrated")
def test_prometheus(traefik_url: str, cos_model: jubilant.Juju):
    """Test that the cluster's metrics reach Prometheus."""
    prometheus = Prometheus(model_name=cos_model.show_model().short_name, base=traefik_url)
    queries = [
        'up{job="etcd"} > 0',
        'up{job="kubelet", metrics_path="/metrics"} > 0',
        'up{job="kubelet", metrics_path="/metrics/resource"} > 0',
        'up{job="kubelet", metrics_path="/metrics/cadvisor"} > 0',
        'up{job="kubelet", metrics_path="/metrics/probes"} > 0',
        'up{job="apiserver"} > 0',
        'up{job="kube-controller-manager"} > 0',
        'up{job="kube-scheduler"} > 0',
        'up{job="kube-proxy"} > 0',
        'up{job="kube-state-metrics"} > 0',
    ]

    def check() -> None:
        assert prometheus.is_ready(), "Prometheus is not ready"
        failed = [query for query in queries if not prometheus.get_metrics(query)]
        assert not failed, f"Queries without results: {failed}"

    _poll(check)
