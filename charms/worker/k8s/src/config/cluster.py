# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Cluster configuration options."""

from typing import Optional

import ops
from config import option

from charms.k8s.v0.k8sd_api_manager import (
    DNSConfig,
    GatewayConfig,
    IngressConfig,
    LoadBalancerConfig,
    LocalStorageConfig,
    MetricsServerConfig,
    NetworkConfig,
    UserFacingClusterConfig,
)


def assemble_cluster_config(
    charm: ops.CharmBase,
    cloud_provider: Optional[str],
    current: Optional[UserFacingClusterConfig] = None,
) -> UserFacingClusterConfig:
    """Retrieve the cluster config from charm configuration and charm relations.

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
    assembled.cloud_provider = cloud_provider
    return assembled


def _assemble_local_storage(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (ls := assembled.local_storage):
        ls = assembled.local_storage = LocalStorageConfig()
    ls.enabled = option.LOCAL_STORAGE_ENABLED.load(charm)
    ls.local_path = option.LOCAL_STORAGE_LOCAL_PATH.load(charm)
    ls.reclaim_policy = option.LOCAL_STORAGE_RECLAIM_POLICY.load(charm)


def _assemble_dns(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (dns := assembled.dns):
        dns = assembled.dns = DNSConfig()
    dns.enabled = option.DNS_ENABLED.load(charm)

    if cfg := option.DNS_CLUSTER_DOMAIN.load(charm):
        dns.cluster_domain = cfg
    if cfg := option.DNS_SERVICE_IP.load(charm):
        dns.service_ip = cfg
    if cfg := option.DNS_UPSTREAM_NAMESERVERS.load(charm):
        dns.upstream_nameservers = cfg.split()
    return dns


def _assemble_gateway(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (gateway := assembled.gateway):
        gateway = assembled.gateway = GatewayConfig()
    gateway.enabled = option.GATEWAY_ENABLED.load(charm)


def _assemble_network(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (network := assembled.network):
        network = assembled.network = NetworkConfig()
    network.enabled = option.NETWORK_ENABLED.load(charm)


def _assemble_ingress(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (ingress := assembled.ingress):
        ingress = assembled.ingress = IngressConfig()
    ingress.enabled = option.INGRESS_ENABLED.load(charm)
    ingress.enable_proxy_protocol = option.INGRESS_ENABLE_PROXY_PROTOCOL.load(charm)


def _assemble_metrics_server(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (metrics_server := assembled.metrics_server):
        metrics_server = assembled.metrics_server = MetricsServerConfig()
    metrics_server.enabled = option.METRICS_SERVER_ENABLED.load(charm)


def _assemble_load_balancer(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (load_balancer := assembled.load_balancer):
        load_balancer = assembled.load_balancer = LoadBalancerConfig()
    load_balancer.enabled = option.LOAD_BALANCER_ENABLED.load(charm)
    load_balancer.cidrs = option.LOAD_BALANCER_CIDRS.load(charm).split()
    load_balancer.l2_mode = option.LOAD_BALANCER_L2_MODE.load(charm)
    load_balancer.l2_interfaces = option.LOAD_BALANCER_L2_INTERFACES.load(charm).split()
    load_balancer.bgp_mode = option.LOAD_BALANCER_BGP_MODE.load(charm)
    load_balancer.bgp_local_asn = option.LOAD_BALANCER_BGP_LOCAL_ASN.load(charm)
    load_balancer.bgp_peer_address = option.LOAD_BALANCER_BGP_PEER_ADDRESS.load(charm)
    load_balancer.bgp_peer_asn = option.LOAD_BALANCER_BGP_PEER_ASN.load(charm)
    load_balancer.bgp_peer_port = option.LOAD_BALANCER_BGP_PEER_PORT.load(charm)
