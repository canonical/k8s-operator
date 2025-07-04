# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Relation kube-control module."""

import logging
from base64 import b64decode

import ops
import yaml
from literals import APISERVER_PORT, BOOTSTRAP_NODE_TAINTS, NODE_LABELS
from protocols import K8sCharmProtocol

import charms.contextual_status as status
from charms.contextual_status import on_error

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


@on_error(
    ops.BlockedStatus(f"Invalid config on {NODE_LABELS.name} or {BOOTSTRAP_NODE_TAINTS.name}"),
    ValueError,
    TypeError,
)
def _share_labels_and_taints(charm: K8sCharmProtocol):
    """Share labels and taints with the kube-control interface.

    Args:
        charm (K8sCharmProtocol): The charm instance.
    """
    labels = NODE_LABELS.get(charm)
    taints = BOOTSTRAP_NODE_TAINTS.get(charm)

    charm.kube_control.set_labels(labels.split())
    charm.kube_control.set_taints(taints.split())


def configure(charm: K8sCharmProtocol):
    """Configure kube-control for the Kubernetes cluster.

    Args:
        charm (K8sCharmProtocol): The charm instance.
    """
    if not (binding := charm.model.get_binding("kube-control")):
        return

    status.add(ops.MaintenanceStatus("Configuring Kube Control"))
    ca_cert, endpoints = "", [f"https://{binding.network.bind_address}:{APISERVER_PORT}"]
    if charm.kubeconfig.exists():
        kubeconfig = yaml.safe_load(charm.kubeconfig.read_text())
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
    charm.kube_control.set_image_registry("rocks.canonical.com/cdk")

    charm.kube_control.set_cluster_name(charm.get_cluster_name())
    charm.kube_control.set_has_external_cloud_provider(charm.xcp.has_xcp)
    _share_labels_and_taints(charm)

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
