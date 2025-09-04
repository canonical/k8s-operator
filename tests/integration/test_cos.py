#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging

import juju.model
import pytest
from tenacity import retry, stop_after_attempt, wait_fixed

from .grafana import Grafana
from .prometheus import Prometheus

log = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.bundle(file="test-bundle-cos.yaml", apps_local=["k8s"]),
    pytest.mark.architecture("amd64"),
]


@pytest.mark.cos
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
async def test_grafana(
    traefik_url: str,
    grafana_password: str,
    expected_dashboard_titles: set,
    cos_model: juju.model.Model,
    timeout: int,
):
    """Test integration with Grafana."""
    grafana = Grafana(model_name=cos_model.name, base=traefik_url, password=grafana_password)
    await asyncio.wait_for(grafana.is_ready(), timeout=timeout * 60)
    dashboards = await grafana.dashboards_all()
    actual_dashboard_titles = set()

    for dashboard in dashboards:
        actual_dashboard_titles.add(dashboard.get("title"))

    assert expected_dashboard_titles.issubset(actual_dashboard_titles)


@pytest.mark.cos
@pytest.mark.usefixtures("related_prometheus")
@retry(reraise=True, stop=stop_after_attempt(12), wait=wait_fixed(60))
async def test_prometheus(traefik_url: str, cos_model: juju.model.Model, timeout: int):
    """Test integration with Prometheus."""
    prometheus = Prometheus(model_name=cos_model.name, base=traefik_url)
    await asyncio.wait_for(prometheus.is_ready(), timeout=timeout * 60)

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
    results = await asyncio.gather(*[prometheus.get_metrics(query) for query in queries])
    failed = [query for query, result in zip(queries, results) if not result]
    assert not failed, f"Failed queries: {failed}"
