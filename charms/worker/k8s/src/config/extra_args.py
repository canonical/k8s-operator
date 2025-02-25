# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Parse extra arguments for Kubernetes components."""
from typing import Dict, Union

import ops
from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    NodeJoinConfig,
)


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


def craft(
    src: ops.ConfigData,
    dest: Union[BootstrapConfig, ControlPlaneNodeJoinConfig, NodeJoinConfig],
    cluster_name: str,
):
    """Set extra arguments for Kubernetes components based on the provided configuration.

    Updates the following attributes of the `config` object:
        - extra_node_kube_apiserver_args: arguments for kube-apiserver.
        - extra_node_kube_controller_manager_args: arguments for kube-controller-manager.
        - extra_node_kube_scheduler_args: arguments for kube-scheduler.
        - extra_node_kube_proxy_args: arguments for kube-proxy.
        - extra_node_kubelet_args: arguments for kubelet.

    Args:
        src (ops.ConfigData): the charm instance to get the configuration from.
        dest (Union[BootstrapConfig, ControlPlaneNodeJoinConfig, NodeJoinConfig]):
            The configuration object to be updated with extra arguments.
        cluster_name (str): the name of the cluster to override in the extra arguments.
    """
    if isinstance(dest, (BootstrapConfig, ControlPlaneNodeJoinConfig)):
        cmd = _parse(src["kube-apiserver-extra-args"])
        dest.extra_node_kube_apiserver_args = cmd

        cmd = _parse(src["kube-controller-manager-extra-args"])
        if cluster_name:
            cmd.update(**{"--cluster-name": cluster_name})
        else:
            cmd.pop("--cluster-name", None)
        dest.extra_node_kube_controller_manager_args = cmd

        cmd = _parse(src["kube-scheduler-extra-args"])
        dest.extra_node_kube_scheduler_args = cmd

    cmd = _parse(src["kube-proxy-extra-args"])
    dest.extra_node_kube_proxy_args = cmd

    cmd = _parse(src["kubelet-extra-args"])
    dest.extra_node_kubelet_args = cmd
