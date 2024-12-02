# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Literals for the charm."""

SNAP_NAME = "k8s"

K8S_COMMON_SERVICES = [
    "kubelet",
    "kube-proxy",
    "k8sd",
]

K8S_CONTROL_PLANE_SERVICES = [
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
]

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
        "dependencies": {"k8s-worker": "^1.30, < 1.32"},
        "name": "k8s",
        "upgrade_supported": "^1.30, < 1.32",
        "version": "1.31.3",
    },
}
