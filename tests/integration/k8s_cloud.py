# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Register a Kubernetes cluster as a Juju cloud.

Replacement for ``pytest_operator.plugin.OpsTest.add_k8s``, which used python-libjuju's
controller facade. Jubilant offers ``add_cloud``/``add_credential``, which take the same
YAML documents the ``juju`` CLI accepts.
"""

import base64
import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import jubilant
import kubernetes.client
from kubernetes.client import Configuration as K8sConfiguration

log = logging.getLogger(__name__)

AUTH_TYPES = ["certificate", "clientcertificate", "oauth2", "oauth2withcert", "userpass"]


def _default_storage_class(kubeconfig: K8sConfiguration) -> Optional[str]:
    """Look up the cluster's default storage class.

    Args:
        kubeconfig: Kubernetes client configuration.

    Returns:
        The name of the default storage class, or None.
    """
    api_client = kubernetes.client.ApiClient(configuration=kubeconfig)
    cluster = kubernetes.client.StorageV1Api(api_client=api_client)
    for sc in cluster.list_storage_class().items:
        annotations = sc.metadata.annotations or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return sc.metadata.name
    return None


def _credential(cloud_name: str, kubeconfig: K8sConfiguration) -> Dict[str, Any]:
    """Build the ``juju add-credential`` document for a Kubernetes cluster.

    Args:
        cloud_name: Name to register the cloud under.
        kubeconfig: Kubernetes client configuration.

    Returns:
        The credential document.

    Raises:
        ValueError: if no usable credentials are present in the kubeconfig.
    """
    if kubeconfig.cert_file and kubeconfig.key_file:
        attrs = {
            "auth-type": "clientcertificate",
            "ClientCertificateData": Path(kubeconfig.cert_file).read_text(),
            "ClientKeyData": Path(kubeconfig.key_file).read_text(),
        }
    elif token := kubeconfig.api_key.get("authorization"):
        if token.startswith("Bearer "):
            attrs = {"auth-type": "oauth2", "Token": token.split(" ", 1)[1]}
        elif token.startswith("Basic "):
            userpass = base64.b64decode(token.split(" ", 1)[1]).decode()
            user, passwd = userpass.split(":", 1)
            attrs = {"auth-type": "userpass", "username": user, "password": passwd}
        else:
            raise ValueError("Failed to find credentials in authorization token")
    else:
        raise ValueError("Failed to find credentials in kubernetes.Configuration")

    return {"credentials": {cloud_name: {cloud_name: attrs}}}


def add_k8s(
    juju: jubilant.Juju,
    cloud_name: str,
    kubeconfig: K8sConfiguration,
    *,
    controller: str,
    skip_storage: bool = True,
    storage_class: Optional[str] = None,
) -> str:
    """Register a Kubernetes cluster as a cloud on a Juju controller.

    Args:
        juju: Jubilant Juju instance (used only to reach the CLI).
        cloud_name: Name to register the cloud under.
        kubeconfig: Kubernetes client configuration for the target cluster.
        controller: Controller to register the cloud with.
        skip_storage: If true, don't configure Juju storage for the cloud.
        storage_class: Storage class to use; looked up when not given.

    Returns:
        The cloud name.
    """
    config: Dict[str, Any] = {}
    if not skip_storage:
        storage_class = storage_class or _default_storage_class(kubeconfig)
        if storage_class:
            config["workload-storage"] = storage_class
            config["operator-storage"] = storage_class

    definition = {
        "clouds": {
            cloud_name: {
                "type": "kubernetes",
                "auth-types": AUTH_TYPES,
                "endpoint": kubeconfig.host,
                "ca-certificates": [Path(kubeconfig.ssl_ca_cert).read_text()],
                "host-cloud-region": "kubernetes/ops-test",
                "regions": {"default": {"endpoint": kubeconfig.host}},
                "skip-tls-verify": not kubeconfig.verify_ssl,
                "config": config,
            }
        }
    }

    log.info("Adding k8s cloud %s to controller %s", cloud_name, controller)
    juju.add_cloud(cloud_name, definition, controller=controller, force=True)
    juju.add_credential(cloud_name, _credential(cloud_name, kubeconfig), controller=controller)
    return cloud_name


def remove_k8s(juju: jubilant.Juju, cloud_name: str, *, controller: str) -> None:
    """Remove a previously registered Kubernetes cloud and its credential.

    Args:
        juju: Jubilant Juju instance (used only to reach the CLI).
        cloud_name: Name the cloud was registered under.
        controller: Controller the cloud was registered with.
    """
    # The cloud and credential were only added to the controller, not to the client, so
    # don't pass --client here.
    for args in (
        ("remove-credential", cloud_name, cloud_name, "--controller", controller, "--force"),
        ("remove-cloud", cloud_name, "--controller", controller),
    ):
        with contextlib.suppress(jubilant.CLIError):
            juju.cli(*args, include_model=False)
