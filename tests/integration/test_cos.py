#!/usr/bin/env python3

# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import logging

import jubilant
import pytest
from grafana import Grafana
from prometheus import Prometheus
from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-bundle-cos.yaml", apps_local=["k8s"]),
    pytest.mark.architecture("amd64"),
]


@pytest.mark.cos
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: jubilant.Juju,
    timeout: int,
):
    """Test integration with Grafana."""
    grafana = Grafana(model_name=cos_model.model, base=traefik_url, password=grafana_password)
    assert grafana.is_ready()
    dashboards = grafana.dashboards_all()
    actual_dashboard_titles = set()

    for dashboard in dashboards:
        actual_dashboard_titles.add(dashboard.get("title"))

    assert expected_dashboard_titles.issubset(actual_dashboard_titles)


@pytest.mark.cos
@pytest.mark.usefixtures("related_prometheus")
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
def test_prometheus(traefik_url: str, cos_model: jubilant.Juju, timeout: int):
    """Test integration with Prometheus."""
    prometheus = Prometheus(model_name=cos_model.model, base=traefik_url)
    assert prometheus.is_ready()

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
    results = [prometheus.get_metrics(query) for query in queries]
    failed = [query for query, result in zip(queries, results) if not result]
    assert not failed, f"Failed queries: {failed}"
