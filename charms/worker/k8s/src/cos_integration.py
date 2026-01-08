# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""COS Integration module."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

import ops
from literals import DATASTORE_TYPE_ETCD
from ops.charm import CharmBase

log = logging.getLogger(__name__)

OBSERVABILITY_ROLE = "system:cos"


@dataclass
class JobConfig:
    """Data class representing the configuration for a Prometheus scrape job.

    Attributes:
        name (str): The name of the scrape job. Corresponds to the name of the Kubernetes
                    component being monitored (e.g., 'kube-apiserver').
        metrics_path (str): The endpoint path where the metrics are exposed by the
                            component (e.g., '/metrics').
        scheme (str): The scheme used for the endpoint. (e.g.'http' or 'https').
        target (str): The network address of the target component along with
                      the port.
                      Format is 'hostname:port' (e.g., 'localhost:6443').
        relabel_configs (List[Dict[str, str | List[str]]]): Additional
                      configurations for relabeling.
        static_configs (Optional[List[Any]]): Static config to override the default ones.
    """

    name: str
    metrics_path: str
    scheme: str
    target: str
    relabel_configs: List[Dict[str, Union[str, Sequence[str]]]]
    static_configs: Optional[List[Any]] = None


class RefreshCOSAgent(ops.EventBase):
    """Event to trigger a refresh of the scrape jobs."""


class COSIntegration(ops.Object):
    """Utility class that handles the integration with COS.

    This class provides methods to retrieve and configure Prometheus metrics
    scraping endpoints based on the Kubernetes components running within
    the cluster.

    Attributes:
        charm (CharmBase): Reference to the base charm instance.
    """

    refresh_event = ops.EventSource(RefreshCOSAgent)

    def __init__(self, charm: CharmBase) -> None:
        """Initialize a COSIntegration instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
        """
        super().__init__(charm, "cos-integration")
        self.charm = charm

    def _create_scrape_job(self, config: JobConfig, node_name: str, token: str) -> dict:
        """Create a scrape job configuration.

        Args:
            config (JobConfig): The configuration for the scrape job.
            node_name (str): The name of the node.
            token (str): The token for authorization.

        Returns:
            dict: The scrape job configuration.
        """
        return {
            "tls_config": {"insecure_skip_verify": True},
            "authorization": {"credentials": token},
            "job_name": config.name,
            "metrics_path": config.metrics_path,
            "scheme": config.scheme,
            "static_configs": config.static_configs
            or [
                {
                    "targets": [config.target],
                    "labels": {"node": node_name, "cluster": self.charm.model.name},
                }
            ],
            "relabel_configs": config.relabel_configs,
        }

    def get_metrics_endpoints(
        self,
        node_name: str,
        token: str,
        control_plane: bool = False,
        datastore: Optional[str] = None,
    ) -> List[Dict]:
        """Retrieve Prometheus scrape job configurations for Kubernetes components.

        Args:
            node_name (str): The name of the node.
            token (str): The authentication token.
            control_plane (bool, optional): If True, include control plane components.
                Defaults to False.
            datastore (Optional[str]): The datastore used in the cluster.

        Returns:
            List[Dict]: A list of Prometheus scrape job configurations.
        """
        log.info("Building Prometheus scraping jobs.")

        instance_relabel = {
            "source_labels": ["instance"],
            "target_label": "instance",
            "replacement": node_name,
        }

        control_plane_jobs = [
            JobConfig(
                "apiserver",
                "/metrics",
                "https",
                "localhost:6443",
                [
                    {
                        "source_labels": ["job"],
                        "target_label": "job",
                        "replacement": "apiserver",
                    },
                    instance_relabel,
                ],
            ),
            JobConfig(
                "kube-scheduler",
                "/metrics",
                "https",
                "localhost:10259",
                [{"target_label": "job", "replacement": "kube-scheduler"}, instance_relabel],
            ),
            JobConfig(
                "kube-controller-manager",
                "/metrics",
                "https",
                "localhost:10257",
                [
                    {"target_label": "job", "replacement": "kube-controller-manager"},
                    instance_relabel,
                ],
            ),
        ]

        shared_jobs = [
            JobConfig(
                "kube-proxy",
                "/metrics",
                "http",
                "localhost:10249",
                [{"target_label": "job", "replacement": "kube-proxy"}, instance_relabel],
            ),
        ]

        kubelet_metrics_paths = [
            "/metrics",
            "/metrics/resource",
            "/metrics/cadvisor",
            "/metrics/probes",
        ]

        kubelet_jobs = [
            JobConfig(
                f"kubelet-{metric}" if metric else "kubelet",
                path,
                "https",
                "localhost:10250",
                [
                    {"target_label": "metrics_path", "replacement": path},
                    {"target_label": "job", "replacement": "kubelet"},
                    instance_relabel,
                ],
            )
            for path in kubelet_metrics_paths
            if (metric := path.strip("/metrics")) is not None
        ]
        kube_state_metrics = (
            [
                JobConfig(
                    "kube-state-metrics",
                    "/api/v1/namespaces/kube-system/services/"
                    + "kube-state-metrics:8080/proxy/metrics",
                    "https",
                    "localhost:6443",
                    [
                        {"target_label": "job", "replacement": "kube-state-metrics"},
                    ],
                    [
                        {
                            "targets": ["localhost:6443"],
                            "labels": {"cluster": self.charm.model.name},
                        }
                    ],
                )
            ]
            if self.charm.unit.is_leader()
            else []
        )

        managed_etcd_jobs = JobConfig(
            "etcd",
            "/metrics",
            "http",
            "localhost:2381",
            [{"target_label": "job", "replacement": "etcd"}, instance_relabel],
            [
                {
                    "targets": ["localhost:2381"],
                    "labels": {"cluster": self.charm.model.name},
                }
            ],
        )

        jobs = shared_jobs + kubelet_jobs
        if control_plane:
            jobs.extend(control_plane_jobs + kube_state_metrics)
            if datastore == DATASTORE_TYPE_ETCD:
                jobs.append(managed_etcd_jobs)

        return [self._create_scrape_job(job, node_name, token) for job in jobs]

    def trigger_jobs_refresh(self):
        """Trigger a custom event to refresh all the scrape jobs."""
        self.refresh_event.emit()
