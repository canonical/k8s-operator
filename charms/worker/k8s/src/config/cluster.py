# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Cluster configuration options."""

import logging
from typing import Dict, Optional

import literals
import ops
from k8sd_api_manager import (
    DNSConfig,
    GatewayConfig,
    IngressConfig,
    LoadBalancerConfig,
    LocalStorageConfig,
    MetricsServerConfig,
    NetworkConfig,
    UserFacingClusterConfig,
)

log = logging.getLogger(__name__)


def assemble_cluster_config(
    charm: ops.CharmBase,
    cloud_provider: Optional[str],
    current: Optional[UserFacingClusterConfig] = None,
    annotations: Optional[Dict[str, str]] = None,
) -> UserFacingClusterConfig:
    """Retrieve the cluster config from charm configuration and charm relations.

    Args:
        charm: The charm instance to read configuration from.
        cloud_provider: The cloud provider to set on the cluster config.
        current: The current cluster config, used as the base to assemble on.
        annotations: Parsed cluster-annotations charm config, or None if unset.

    Returns:
        UserFacingClusterConfig: The expected cluster configuration.
    """
    if not current:
        assembled = UserFacingClusterConfig()
    else:
        assembled = current.model_copy(deep=True)

    _assemble_local_storage(charm, assembled)
    _assemble_dns(charm, assembled)
    _assemble_gateway(charm, assembled)
    _assemble_network(charm, assembled)
    _assemble_ingress(charm, assembled)
    _assemble_metrics_server(charm, assembled)
    _assemble_load_balancer(charm, assembled)
    _assemble_annotations(annotations, assembled)
    assembled.cloud_provider = cloud_provider
    return assembled


def _assemble_local_storage(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (ls := assembled.local_storage):
        ls = assembled.local_storage = LocalStorageConfig()
    ls.enabled = literals.LOCAL_STORAGE_ENABLED.get(charm)
    ls.local_path = literals.LOCAL_STORAGE_LOCAL_PATH.get(charm)
    ls.reclaim_policy = literals.LOCAL_STORAGE_RECLAIM_POLICY.get(charm)


def _assemble_dns(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (dns := assembled.dns):
        dns = assembled.dns = DNSConfig()
    dns.enabled = literals.DNS_ENABLED.get(charm)

    if cfg := literals.DNS_CLUSTER_DOMAIN.get(charm):
        dns.cluster_domain = cfg
    if cfg := literals.DNS_SERVICE_IP.get(charm):
        dns.service_ip = cfg
    if cfg := literals.DNS_UPSTREAM_NAMESERVERS.get(charm):
        dns.upstream_nameservers = cfg.split()
    return dns


def _assemble_gateway(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (gateway := assembled.gateway):
        gateway = assembled.gateway = GatewayConfig()
    gateway.enabled = literals.GATEWAY_ENABLED.get(charm)


def _assemble_network(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (network := assembled.network):
        network = assembled.network = NetworkConfig()
    network.enabled = literals.NETWORK_ENABLED.get(charm)

    kube_proxy_enabled = literals.KUBE_PROXY_ENABLED.get(charm).lower()
    if kube_proxy_enabled not in literals.KUBE_PROXY_ENABLED_VALID_VALUES:
        log.error(f"invalid value for kube-proxy-enabled config option: {kube_proxy_enabled}")

    if kube_proxy_enabled == literals.KUBE_PROXY_ENABLED_TRUE:
        network.kube_proxy_enabled = True
        log.info("kube_proxy_enabled option is set to True in user-facing cluster config")
    elif kube_proxy_enabled == literals.KUBE_PROXY_ENABLED_FALSE:
        network.kube_proxy_enabled = False
        log.info("kube_proxy_enabled option is set to False in user-facing cluster config")
    elif kube_proxy_enabled == literals.KUBE_PROXY_ENABLED_AUTO:
        # we do not set network.kube_proxy_enabled when the config option is "auto"
        # so that the cluster decides automatically.
        log.info(
            "kube_proxy_enabled option is set to auto."
            "Will not set it in user-facing cluster config"
        )


def _assemble_ingress(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (ingress := assembled.ingress):
        ingress = assembled.ingress = IngressConfig()
    ingress.enabled = literals.INGRESS_ENABLED.get(charm)
    ingress.enable_proxy_protocol = literals.INGRESS_ENABLE_PROXY_PROTOCOL.get(charm)


def _assemble_metrics_server(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (metrics_server := assembled.metrics_server):
        metrics_server = assembled.metrics_server = MetricsServerConfig()
    metrics_server.enabled = literals.METRICS_SERVER_ENABLED.get(charm)


def _assemble_load_balancer(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (load_balancer := assembled.load_balancer):
        load_balancer = assembled.load_balancer = LoadBalancerConfig()
    load_balancer.enabled = literals.LOAD_BALANCER_ENABLED.get(charm)
    load_balancer.cidrs = literals.LOAD_BALANCER_CIDRS.get(charm).split()
    load_balancer.l2_mode = literals.LOAD_BALANCER_L2_MODE.get(charm)
    load_balancer.l2_interfaces = literals.LOAD_BALANCER_L2_INTERFACES.get(charm).split()
    load_balancer.bgp_mode = literals.LOAD_BALANCER_BGP_MODE.get(charm)
    load_balancer.bgp_local_asn = literals.LOAD_BALANCER_BGP_LOCAL_ASN.get(charm)
    load_balancer.bgp_peer_address = literals.LOAD_BALANCER_BGP_PEER_ADDRESS.get(charm)
    load_balancer.bgp_peer_asn = literals.LOAD_BALANCER_BGP_PEER_ASN.get(charm)
    load_balancer.bgp_peer_port = literals.LOAD_BALANCER_BGP_PEER_PORT.get(charm)


def _assemble_annotations(
    annotations: Optional[Dict[str, str]], assembled: UserFacingClusterConfig
):
    """Merge cluster annotations from the cluster-annotations charm config.

    Mirrors k8sd's merge semantics: the charm-managed annotations are patched
    onto the existing cluster annotations, so annotations set out-of-band (e.g.
    via the k8s CLI) are preserved and repeated reconciliations converge instead
    of reporting perpetual config changes. A value of "-" removes the
    annotation from the cluster; the deletion marker is forwarded to k8sd only
    while the key is still present in the stored configuration.

    Args:
        annotations: Parsed cluster-annotations charm config, or None if unset.
        assembled: The cluster config being assembled, pre-populated with the
            current cluster config when updating an existing cluster.
    """
    if annotations is None:
        return

    existing = dict(assembled.annotations or {})
    merged = dict(existing)
    for key, value in annotations.items():
        if value == "-":
            merged.pop(key, None)
        else:
            merged[key] = value
    # Forward deletion markers for keys still present in the stored config so
    # that k8sd removes them; markers for absent keys are dropped to keep
    # repeated reconciliations idempotent.
    for key, value in annotations.items():
        if value == "-" and key in existing:
            merged[key] = value
    if merged:
        assembled.annotations = merged
