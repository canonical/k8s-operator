# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Literals for the charm."""

from pathlib import Path

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
SUPPORTED_DATASTORES = ["dqlite", "etcd"]
EXTERNAL_LOAD_BALANCER_REQUEST_NAME = "api-server-external"
EXTERNAL_LOAD_BALANCER_RESPONSE_NAME = EXTERNAL_LOAD_BALANCER_REQUEST_NAME
EXTERNAL_LOAD_BALANCER_PORT = 443
APISERVER_PORT = 6443

# Features
SUPPORT_SNAP_INSTALLATION_OVERRIDE = True

# Relations
CLUSTER_RELATION = "cluster"
CLUSTER_WORKER_RELATION = "k8s-cluster"
CONTAINERD_RELATION = "containerd"
COS_TOKENS_RELATION = "cos-tokens"
COS_TOKENS_WORKER_RELATION = "cos-worker-tokens"
COS_RELATION = "cos-agent"
ETCD_RELATION = "etcd"
UPGRADE_RELATION = "upgrade"
EXTERNAL_LOAD_BALANCER_RELATION = "external-load-balancer"

# Kubernetes services
K8S_COMMON_SERVICES = [
    "kubelet",
    "kube-proxy",
    "k8sd",
]

K8S_DQLITE_SERVICE = "k8s-dqlite"

K8S_CONTROL_PLANE_SERVICES = [
    "kube-apiserver",
    K8S_DQLITE_SERVICE,
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
        "dependencies": {"k8s-worker": "^1.31, < 1.33"},
        "name": "k8s",
        "upgrade_supported": "^1.31, < 1.33",
        "version": "1.32.0",
    },
}
