"""COS Integration module."""

import logging
from dataclasses import dataclass
from typing import Dict, List

import ops

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
    """

    name: str
    metrics_path: str
    scheme: str
    target: str
    relabel_configs: List[Dict[str, str | List[str]]]


class COSIntegration(ops.Object):
    """Utility class that handles the integration with COS.

    This class provides methods to retrieve and configure Prometheus metrics
    scraping endpoints based on the Kubernetes components running within
    the cluster.

    Attributes:
        charm (CharmBase): Reference to the base charm instance.
    """

    _stored = ops.StoredState()

    def __init__(self, charm) -> None:
        """Initialize a COSIntegration instance.

        Args:
            charm (CharmBase): A charm object representing the current charm.
        """
        super().__init__(charm, "cos-integration")
        self.charm = charm
        self._stored.set_default(token="")

    def save_token(self, token: str):
        """Save the token in the StoredState.

        Args:
            token (str): A token to save in the StoredState instance.
        """
        self._stored.token = token

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
            "static_configs": [
                {
                    "targets": [config.target],
                    "labels": {"node": node_name, "cluster": self.charm.model.name},
                }
            ],
            "relabel_configs": config.relabel_configs,
        }

    def get_metrics_endpoints(self) -> list:
        """Return the metrics endpoints for K8s components.

        Returns:
            list: A list of Prometheus scrape job configurations.
        """
        log.info("Building Prometheus scraping jobs.")

        if not self._stored.token:
            log.info("COS token not yet available")
            return []

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
                    }
                ],
            ),
            JobConfig(
                "kube-scheduler",
                "/metrics",
                "https",
                "localhost:10259",
                [{"target_label": "job", "replacement": "kube-scheduler"}],
            ),
            JobConfig(
                "kube-controller-manager",
                "/metrics",
                "https",
                "localhost:10257",
                [{"target_label": "job", "replacement": "kube-controller-manager"}],
            ),
        ]

        shared_jobs = [
            JobConfig(
                "kube-proxy",
                "/metrics",
                "http",
                "localhost:10249",
                [{"target_label": "job", "replacement": "kube-proxy"}],
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
                )
            ]
            if self.charm.unit.is_leader()
            else []
        )

        jobs = shared_jobs + kubelet_jobs
        if self.charm.is_control_plane:
            jobs += control_plane_jobs + kube_state_metrics

        return [
            self._create_scrape_job(job, self.charm.get_node_name(), self._stored.token)
            for job in jobs
        ]
