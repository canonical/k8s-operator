# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Cluster configuration options."""

import logging
from typing import Optional

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
    _assemble_annotations(charm, assembled)
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


def _assemble_annotations(charm: ops.CharmBase, assembled: UserFacingClusterConfig):
    """Populate annotations from the cluster-annotations charm config.

    Annotations are only overwritten when the charm config is non-empty.
    When the config is unset the current cluster annotations are left intact so
    that out-of-band annotation changes (e.g. made directly via the k8s CLI)
    are not silently discarded on the next reconcile cycle.
    """
    raw = charm.config.get("cluster-annotations", "")
    if not raw:
        return

    import yaml  # local import — yaml is a standard library dep of ops

    try:
        parsed = yaml.safe_load(str(raw))
    except yaml.YAMLError:
        log.warning("cluster-annotations is not valid YAML — skipping annotation assembly")
        return

    if not isinstance(parsed, dict):
        log.warning(
            "cluster-annotations must be a YAML mapping — got %s, skipping",
            type(parsed).__name__,
        )
        return

    assembled.annotations = {str(k): str(v) for k, v in parsed.items()}
