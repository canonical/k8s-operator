# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Relation kube-control module."""
import logging
from base64 import b64decode

import charms.contextual_status as status
import ops
import yaml
from protocols import K8sCharmProtocol

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


def configure(charm: K8sCharmProtocol):
    """Configure kube-control for the Kubernetes cluster.

    Args:
        charm (K8sCharmProtocol): The charm instance.
    """
    if not (binding := charm.model.get_binding("kube-control")):
        return

    status.add(ops.MaintenanceStatus("Configuring Kube Control"))
    ca_cert, endpoints = "", [f"https://{binding.network.bind_address}:6443"]
    labels = str(charm.model.config["node-labels"])
    taints = str(charm.model.config["bootstrap-node-taints"])
    if charm._internal_kubeconfig.exists():
        kubeconfig = yaml.safe_load(charm._internal_kubeconfig.read_text())
        cluster = kubeconfig["clusters"][0]["cluster"]
        ca_cert_b64 = cluster["certificate-authority-data"]
        ca_cert = b64decode(ca_cert_b64).decode("utf-8")

    charm.kube_control.set_api_endpoints(endpoints)
    charm.kube_control.set_ca_certificate(ca_cert)

    if (
        (cluster_status := charm.api_manager.get_cluster_status())
        and cluster_status.metadata
        and cluster_status.metadata.status.config
        and (dns := cluster_status.metadata.status.config.dns)
    ):
        charm.kube_control.set_dns_address(dns.service_ip or "")
        charm.kube_control.set_dns_domain(dns.cluster_domain or "")
        charm.kube_control.set_dns_enabled(dns.enabled)
        charm.kube_control.set_dns_port(53)

    charm.kube_control.set_default_cni("")
    charm.kube_control.set_image_registry("rocks.canonical.com")

    charm.kube_control.set_cluster_name(charm.get_cluster_name())
    charm.kube_control.set_has_external_cloud_provider(charm.xcp.has_xcp)
    charm.kube_control.set_labels(labels.split())
    charm.kube_control.set_taints(taints.split())

    for request in charm.kube_control.auth_requests:
        log.info("Signing kube-control request for '%s 'in '%s'", request.user, request.group)
        client_token = charm.api_manager.request_auth_token(
            username=request.user, groups=[request.group]
        )
        charm.kube_control.sign_auth_request(
            request,
            client_token=client_token.get_secret_value(),
            kubelet_token=str(),
            proxy_token=str(),
        )

    for user, cred in charm.kube_control.closed_auth_creds():
        log.info("Revoke auth-token for '%s'", user)
        charm.api_manager.revoke_auth_token(cred.load_client_token(charm.model, user))
