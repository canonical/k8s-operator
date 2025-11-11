# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Parse extra arguments for Kubernetes components."""

from typing import Dict, List, Optional, Union

import literals
import ops
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    NodeJoinConfig,
)
from config.arg_files import FileArgsConfig


def _parse(config_data) -> Dict[str, str]:
    """Parse user config data into a dictionary.

    Args:
        config_data: the charm config data for an extra-args
    """
    args: Dict[str, str] = {}
    for element in str(config_data).split():
        if "=" in element:
            key, _, value = element.partition("=")
        else:
            key, value = element, "true"
        if value is not None:
            args["--" + key.lstrip("-")] = value
    return args


def _configure_datastore_args(dest, src, datastore: Optional[str]):
    """Configure Service Arguments for the specified datastore."""
    if not datastore:
        return

    datastore_args = _parse(src["datastore-extra-args"])
    if datastore == literals.DATASTORE_TYPE_K8S_DQLITE:
        dest.extra_node_k8s_dqlite_args = datastore_args
    elif datastore == literals.DATASTORE_TYPE_ETCD:
        # NOTE: Enable the metrics URL on localhost.
        datastore_args[literals.ETCD_LISTEN_METRICS_URLS_ARG] = literals.ETCD_DEFAULT_METRICS_URL
        dest.extra_node_etcd_args = datastore_args


def craft(
    src: ops.ConfigData,
    dest: Union[BootstrapConfig, ControlPlaneNodeJoinConfig, FileArgsConfig, NodeJoinConfig],
    cluster_name: str,
    node_ips: List[str],
    datastore: Optional[str] = None,
):
    """Set extra arguments for Kubernetes components based on the provided configuration.

    Updates the following attributes of the `config` object:
        - extra_node_kube_apiserver_args: arguments for kube-apiserver.
        - extra_node_kube_controller_manager_args: arguments for kube-controller-manager.
        - extra_node_kube_scheduler_args: arguments for kube-scheduler.
        - extra_node_kube_proxy_args: arguments for kube-proxy.
        - extra_node_kubelet_args: arguments for kubelet.
        - extra_node_etcd_args: arguments for etcd.
        - extra_node_k8s_dqlite_args: arguments for k8s-dqlite

    Args:
        src (ops.ConfigData): the charm instance to get the configuration from.
        dest (Union[BootstrapConfig, ControlPlaneNodeJoinConfig, FileArgsConfig, NodeJoinConfig]):
            The configuration object to be updated with extra arguments.
        cluster_name (str): the name of the cluster to override in the extra arguments.
        node_ips (list[str]): the IP address of the node to override in the extra arguments.
        datastore(Optional[str]): the name of the datastore, specified in charm notation.
    """
    if isinstance(dest, (BootstrapConfig, ControlPlaneNodeJoinConfig)):
        dest.extra_node_kube_apiserver_args = _parse(src["kube-apiserver-extra-args"])

        args = _parse(src["kube-controller-manager-extra-args"])
        if cluster_name:
            args.update(**{"--cluster-name": cluster_name})
        else:
            args.pop("--cluster-name", None)
        dest.extra_node_kube_controller_manager_args = args

        dest.extra_node_kube_scheduler_args = _parse(src["kube-scheduler-extra-args"])

        _configure_datastore_args(dest, src, datastore)

    if isinstance(dest, FileArgsConfig):
        _configure_datastore_args(dest, src, datastore)

    dest.extra_node_kube_proxy_args = _parse(src["kube-proxy-extra-args"])

    args = _parse(src["kubelet-extra-args"])
    if node_ips:
        args.update(**{"--node-ip": ",".join(node_ips)})
    else:
        args.pop("--node-ip", None)
    dest.extra_node_kubelet_args = args


def taint_worker(dest: NodeJoinConfig, taints: List[str]):
    """Apply the specified list of taints to the node join configuration.

    Updates the following attributes of the `config` object:
        - extra_node_kubelet_args: arguments for kubelet.

    Args:
        dest (NodeJoinConfig):
            The configuration object to be updated with extra arguments.
        taints (List[str]):
            The list of taints to apply.
    """
    dest.extra_node_kubelet_args["--register-with-taints"] = ",".join(taints)
