# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the COS scrape-job generation."""

import contextlib
from unittest import mock

import pytest
from literals import COS_TOKENS_RELATION, DATASTORE_TYPE_ETCD, DATASTORE_TYPE_EXTERNAL

NODE = "node-0"
TOKEN = "s3cr3t"

WORKER_JOBS = {"kube-proxy", "kubelet"}
CONTROL_PLANE_JOBS = {"apiserver", "kube-scheduler", "kube-controller-manager"}
KUBELET_PATHS = {"/metrics", "/metrics/resource", "/metrics/cadvisor", "/metrics/probes"}


def _scraped_jobs(jobs):
    """Map the ``job`` label each scrape job ends up with to the jobs that set it.

    The job_name is only an internal identifier; the label prometheus queries by is set
    through relabelling.
    """
    labelled = {}
    for job in jobs:
        replacement = next(
            cfg["replacement"]
            for cfg in job["relabel_configs"]
            if cfg.get("target_label") == "job"
        )
        labelled.setdefault(replacement, []).append(job)
    return labelled


@pytest.fixture()
def cos(harness):
    """Return the COS integration of the charm under test."""
    return harness.charm.cos


def test_worker_scrapes_node_components_only(cos):
    """Workers scrape kube-proxy and the four kubelet endpoints, nothing else."""
    jobs = cos.get_metrics_endpoints(NODE, TOKEN, control_plane=False)
    by_job = _scraped_jobs(jobs)

    assert set(by_job) == WORKER_JOBS
    assert {job["metrics_path"] for job in by_job["kubelet"]} == KUBELET_PATHS


@pytest.mark.parametrize("leader", [True, False], ids=["leader", "follower"])
def test_control_plane_adds_cluster_components(harness, cos, leader):
    """Control planes add the API server, scheduler and controller manager."""
    harness.set_leader(leader)
    jobs = cos.get_metrics_endpoints(NODE, TOKEN, control_plane=True)

    expected = WORKER_JOBS | CONTROL_PLANE_JOBS
    if leader:
        expected = expected | {"kube-state-metrics"}
    assert set(_scraped_jobs(jobs)) == expected


@pytest.mark.parametrize(
    "datastore, has_etcd",
    [
        (DATASTORE_TYPE_ETCD, True),
        (DATASTORE_TYPE_EXTERNAL, False),
        (None, False),
    ],
    ids=["managed-etcd", "external-etcd", "unset"],
)
def test_etcd_scraped_only_for_managed_etcd(cos, datastore, has_etcd):
    """Only the managed etcd runs on the control plane and is scraped locally."""
    jobs = cos.get_metrics_endpoints(NODE, TOKEN, control_plane=True, datastore=datastore)
    assert ("etcd" in _scraped_jobs(jobs)) is has_etcd


def test_workers_never_scrape_etcd(cos):
    """A worker never scrapes etcd, even when the cluster datastore is managed etcd."""
    jobs = cos.get_metrics_endpoints(
        NODE, TOKEN, control_plane=False, datastore=DATASTORE_TYPE_ETCD
    )
    assert "etcd" not in _scraped_jobs(jobs)


def test_every_job_authenticates_with_the_token(harness, cos):
    """Every job presents the token the control plane handed out."""
    harness.set_leader(True)
    jobs = cos.get_metrics_endpoints(
        NODE, TOKEN, control_plane=True, datastore=DATASTORE_TYPE_ETCD
    )
    assert jobs
    assert all(job["authorization"] == {"credentials": TOKEN} for job in jobs)


def test_node_jobs_report_the_node_as_instance(cos):
    """Per-node jobs relabel ``instance`` to the node name, so series are per node."""
    jobs = cos.get_metrics_endpoints(NODE, TOKEN, control_plane=True)
    for job in jobs:
        replacements = [
            cfg["replacement"]
            for cfg in job["relabel_configs"]
            if cfg.get("target_label") == "instance"
        ]
        assert replacements == [NODE], job["job_name"]


def test_kube_state_metrics_is_scraped_through_the_api_server_proxy(harness, cos):
    """kube-state-metrics has no node endpoint, so it is reached via the API server proxy."""
    harness.set_leader(True)
    jobs = cos.get_metrics_endpoints(NODE, TOKEN, control_plane=True)
    (ksm,) = _scraped_jobs(jobs)["kube-state-metrics"]
    assert ksm["metrics_path"].endswith("kube-state-metrics:8080/proxy/metrics")
    assert ksm["static_configs"][0]["targets"] == ["localhost:6443"]


def test_scrape_jobs_empty_without_cos_tokens(harness):
    """Without the cos-tokens relation there is no token and so nothing to scrape."""
    assert harness.charm._get_scrape_jobs() == []


def test_scrape_jobs_use_collected_token(harness):
    """With the cos-tokens relation, jobs are built with the recovered token."""
    harness.add_relation(COS_TOKENS_RELATION, "k8s")
    harness.set_leader(True)

    @contextlib.contextmanager
    def recover_token(_relation):
        yield TOKEN

    with (
        mock.patch.object(harness.charm.collector, "recover_token", recover_token),
        mock.patch.object(harness.charm, "get_node_name", return_value=NODE),
        mock.patch.object(harness.charm.bootstrap.config, "datastore", DATASTORE_TYPE_ETCD),
    ):
        jobs = harness.charm._get_scrape_jobs()

    assert jobs
    assert {job["authorization"]["credentials"] for job in jobs} == {TOKEN}
    assert "kube-proxy" in _scraped_jobs(jobs)
