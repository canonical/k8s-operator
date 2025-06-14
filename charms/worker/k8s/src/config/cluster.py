# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Cluster configuration options."""

from typing import Optional

import ops

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
    ls.enabled = bool(charm.config["local-storage-enabled"])
    ls.local_path = str(charm.config["local-storage-local-path"])
    ls.reclaim_policy = str(charm.config["local-storage-reclaim-policy"])


def _assemble_dns(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (dns := assembled.dns):
        dns = assembled.dns = DNSConfig()
    dns.enabled = bool(charm.config["dns-enabled"])

    if cfg := charm.config["dns-cluster-domain"]:
        dns.cluster_domain = str(cfg)
    if cfg := charm.config["dns-service-ip"]:
        dns.service_ip = str(cfg)
    if cfg := charm.config["dns-upstream-nameservers"]:
        dns.upstream_nameservers = str(cfg).split()
    return dns


def _assemble_gateway(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (gateway := assembled.gateway):
        gateway = assembled.gateway = GatewayConfig()
    gateway.enabled = bool(charm.config["gateway-enabled"])


def _assemble_network(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (network := assembled.network):
        network = assembled.network = NetworkConfig()
    network.enabled = bool(charm.config["network-enabled"])


def _assemble_ingress(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (ingress := assembled.ingress):
        ingress = assembled.ingress = IngressConfig()
    ingress.enabled = bool(charm.config["ingress-enabled"])
    ingress.enable_proxy_protocol = bool(charm.config["ingress-enable-proxy-protocol"])


def _assemble_metrics_server(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (metrics_server := assembled.metrics_server):
        metrics_server = assembled.metrics_server = MetricsServerConfig()
    metrics_server.enabled = bool(charm.config["metrics-server-enabled"])


def _assemble_load_balancer(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    if not (load_balancer := assembled.load_balancer):
        load_balancer = assembled.load_balancer = LoadBalancerConfig()
    load_balancer.enabled = bool(charm.config["load-balancer-enabled"])
    load_balancer.cidrs = str(charm.config["load-balancer-cidrs"]).split()
    load_balancer.l2_mode = bool(charm.config["load-balancer-l2-mode"])
    load_balancer.l2_interfaces = str(charm.config["load-balancer-l2-interfaces"]).split()
    load_balancer.bgp_mode = bool(charm.config["load-balancer-bgp-mode"])
    load_balancer.bgp_local_asn = int(charm.config["load-balancer-bgp-local-asn"])
    load_balancer.bgp_peer_address = str(charm.config["load-balancer-bgp-peer-address"])
    load_balancer.bgp_peer_asn = int(charm.config["load-balancer-bgp-peer-asn"])
    load_balancer.bgp_peer_port = int(charm.config["load-balancer-bgp-peer-port"])
