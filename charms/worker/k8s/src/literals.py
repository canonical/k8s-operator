# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Literals for the charm."""

from pathlib import Path

import config.option as option

# Snap
SNAP_NAME = "k8s"

# Logging
VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]

# Charm
SNAP_COMMON = "/var/snap/k8s/common"
CONTAINERD_ARGS = Path(SNAP_COMMON) / "args/containerd"
CONTAINERD_SERVICE_NAME = "snap.k8s.containerd.service"
CONTAINERD_HTTP_PROXY = Path(f"/etc/systemd/system/{CONTAINERD_SERVICE_NAME}.d/http-proxy.conf")
ETC_KUBERNETES = Path("/etc/kubernetes")
PKI_DIR = ETC_KUBERNETES / "pki"
APISERVER_CERT = PKI_DIR / "apiserver.crt"
KUBECONFIG = Path.home() / ".kube/config"
KUBECTL_PATH = Path("/snap/k8s/current/bin/kubectl")
K8SD_SNAP_SOCKET = f"{SNAP_COMMON}/var/lib/k8sd/state/control.socket"
K8SD_PORT = 6400
SUPPORTED_DATASTORES = ["managed-etcd", "dqlite", "etcd"]
EXTERNAL_LOAD_BALANCER_REQUEST_NAME = "api-server-external"
EXTERNAL_LOAD_BALANCER_RESPONSE_NAME = EXTERNAL_LOAD_BALANCER_REQUEST_NAME
EXTERNAL_LOAD_BALANCER_PORT = 443
APISERVER_PORT = 6443

# Control-Plane Options
BOOTSTRAP_DATASTORE = option.StrOption("bootstrap-datastore")
BOOTSTRAP_CERTIFICATES = option.StrOption("bootstrap-certificates")
BOOTSTRAP_POD_CIDR = option.StrOption("bootstrap-pod-cidr")
BOOTSTRAP_SERVICE_CIDR = option.StrOption("bootstrap-service-cidr")
DNS_ENABLED = option.BoolOption("dns-enabled")
DNS_CLUSTER_DOMAIN = option.StrOption("dns-cluster-domain")
DNS_SERVICE_IP = option.StrOption("dns-service-ip")
DNS_UPSTREAM_NAMESERVERS = option.StrOption("dns-upstream-nameservers")
GATEWAY_ENABLED = option.BoolOption("gateway-enabled")
INGRESS_ENABLED = option.BoolOption("ingress-enabled")
INGRESS_ENABLE_PROXY_PROTOCOL = option.BoolOption("ingress-enable-proxy-protocol")
LOAD_BALANCER_ENABLED = option.BoolOption("load-balancer-enabled")
LOAD_BALANCER_CIDRS = option.StrOption("load-balancer-cidrs")
LOAD_BALANCER_L2_MODE = option.BoolOption("load-balancer-l2-mode")
LOAD_BALANCER_L2_INTERFACES = option.StrOption("load-balancer-l2-interfaces")
LOAD_BALANCER_BGP_MODE = option.BoolOption("load-balancer-bgp-mode")
LOAD_BALANCER_BGP_LOCAL_ASN = option.IntOption("load-balancer-bgp-local-asn")
LOAD_BALANCER_BGP_PEER_ADDRESS = option.StrOption("load-balancer-bgp-peer-address")
LOAD_BALANCER_BGP_PEER_ASN = option.IntOption("load-balancer-bgp-peer-asn")
LOAD_BALANCER_BGP_PEER_PORT = option.IntOption("load-balancer-bgp-peer-port")
LOCAL_STORAGE_ENABLED = option.BoolOption("local-storage-enabled")
LOCAL_STORAGE_LOCAL_PATH = option.StrOption("local-storage-local-path")
LOCAL_STORAGE_RECLAIM_POLICY = option.StrOption("local-storage-reclaim-policy")
NETWORK_ENABLED = option.BoolOption("network-enabled")
METRICS_SERVER_ENABLED = option.BoolOption("metrics-server-enabled")

# Managed ETCD Options
BOOTSTRAP_ETCD_PORT = option.IntOption("bootstrap-etcd-port")
BOOTSTRAP_ETCD_PEER_PORT = option.IntOption("bootstrap-etcd-peer-port")


# Worker and Control Plane Options
BOOTSTRAP_NODE_TAINTS = option.StrOption("bootstrap-node-taints")
NODE_LABELS = option.StrOption("node-labels")

# Features
SUPPORT_SNAP_INSTALLATION_OVERRIDE = True

# Relations
CERTIFICATES_RELATION = "certificates"
CLUSTER_RELATION = "cluster"
CLUSTER_WORKER_RELATION = "k8s-cluster"
CONTAINERD_RELATION = "containerd"
COS_TOKENS_RELATION = "cos-tokens"
COS_TOKENS_WORKER_RELATION = "cos-worker-tokens"
COS_RELATION = "cos-agent"
ETCD_RELATION = "etcd"
UPGRADE_RELATION = "upgrade"
EXTERNAL_LOAD_BALANCER_RELATION = "external-load-balancer"

# Cluster Relation Keys
CLUSTER_CERTIFICATES_KEY = "certs-provider"
CLUSTER_CERTIFICATES_KUBELET_FORMATTER_KEY = "certs-kubelet-formatter"
CLUSTER_CERTIFICATES_DOMAIN_NAME_KEY = "certs-domain-name"

# Certificates
APISERVER_CN_FORMATTER_CONFIG_KEY = "external-certs-apiserver-common-name-format"
KUBELET_CN_FORMATTER_CONFIG_KEY = "external-certs-kubelet-common-name-format"
COMMON_NAME_CONFIG_KEY = "external-certs-domain-name"
MAX_COMMON_NAME_SIZE = 64
SUPPORTED_CERTIFICATES = ["external", "self-signed"]

APISERVER_CSR_KEY = "apiserver"
KUBELET_CSR_KEY = "kubelet"

WORKER_CERTIFICATES = ["kubelet", "kubelet-client", "proxy"]
CONTROL_PLANE_CERTIFICATES = [
    "apiserver",
    "front-proxy-client",
    "admin",
    "controller",
    "scheduler",
] + WORKER_CERTIFICATES
LEADER_CONTROL_PLANE_CERTIFICATES = ["apiserver-kubelet-client"] + CONTROL_PLANE_CERTIFICATES


# Kubernetes services
K8S_COMMON_SERVICES = [
    "kubelet",
    "kube-proxy",
    "k8sd",
]

K8S_DQLITE_SERVICE = "k8s-dqlite"
MANAGED_ETCD_SERVICE = "etcd" 

K8S_CONTROL_PLANE_SERVICES = [
    "kube-apiserver",
    K8S_DQLITE_SERVICE,
    MANAGED_ETCD_SERVICE,
    "kube-controller-manager",
    "kube-scheduler",
    *K8S_COMMON_SERVICES,
]

K8S_WORKER_SERVICES = [
    "k8s-apiserver-proxy",
    *K8S_COMMON_SERVICES,
]

# Upgrade
DEPENDENCIES = {
    # NOTE: Update the dependencies for the k8s-charm before releasing.
    "k8s_charm": {
        "dependencies": {"k8s-worker": ">2"},
        "name": "k8s",
        "upgrade_supported": ">=1",
        "version": "2",
    },
    # NOTE: Update the dependencies for the k8s-service before releasing.
    "k8s_service": {
        "dependencies": {"k8s-worker": "^1.32, < 1.34"},
        "name": "k8s",
        "upgrade_supported": "^1.32, < 1.34",
        "version": "1.33.0",
    },
}
