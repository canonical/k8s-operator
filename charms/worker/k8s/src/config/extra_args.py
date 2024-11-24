# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Parse extra arguments for Kubernetes components."""
from typing import Dict, Union

from charms.k8s.v0.k8sd_api_manager import (
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    NodeJoinConfig,
)
from protocols import K8sCharmProtocol


class ArgMapper:
    """A class to map string key,val pairs to be used as cmd args.

    Attributes:
        args: the args to be used made into command args
    """

    def __init__(self, config_data):
        """Initialise the ArgMapper class.

        Args:
            config_data: the charm config data for an extra-args
        """
        self.args: Dict[str, str] = {}
        for element in str(config_data).split():
            if "=" in element:
                key, _, value = element.partition("=")
            else:
                key, value = element, "true"
            self.args[key.lstrip("-")] = value

    def dict(self) -> Dict[str, str]:
        """Return an args based representative view.

        Returns:
            Dict[str, str]: the args as a dictionary
        """
        return {f"--{k}": v for k, v in self.args.items() if v is not None}


def craft(
    charm: K8sCharmProtocol,
    config: Union[BootstrapConfig, ControlPlaneNodeJoinConfig, NodeJoinConfig],
):
    """Set extra arguments for Kubernetes components based on the provided configuration.

    Updates the following attributes of the `config` object:
        - extra_node_kube_apiserver_args: arguments for kube-apiserver.
        - extra_node_kube_controller_manager_args: arguments for kube-controller-manager.
        - extra_node_kube_scheduler_args: arguments for kube-scheduler.
        - extra_node_kube_proxy_args: arguments for kube-proxy.
        - extra_node_kubelet_args: arguments for kubelet.

    Args:
        charm (ops.CharmBase): the charm instance to get the configuration from.
        config (Union[BootstrapConfig, ControlPlaneNodeJoinConfig, NodeJoinConfig]):
            The configuration object to be updated with extra arguments.
    """
    if isinstance(config, (BootstrapConfig, ControlPlaneNodeJoinConfig)):
        cmd = ArgMapper(charm.config["kube-apiserver-extra-args"])
        config.extra_node_kube_apiserver_args = cmd.dict()

        cmd = ArgMapper(charm.config["kube-controller-manager-extra-args"])
        if cluster_name := charm.get_cluster_name():
            cmd.args.update(**{"cluster-name": cluster_name})
        else:
            cmd.args.pop("cluster-name", None)
        config.extra_node_kube_controller_manager_args = cmd.dict()

        cmd = ArgMapper(charm.config["kube-scheduler-extra-args"])
        config.extra_node_kube_scheduler_args = cmd.dict()

    cmd = ArgMapper(charm.config["kube-proxy-extra-args"])
    config.extra_node_kube_proxy_args = cmd.dict()

    cmd = ArgMapper(charm.config["kubelet-extra-args"])
    config.extra_node_kubelet_args = cmd.dict()
