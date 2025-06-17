# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Accessor for charm config options in the right type."""

from typing import TYPE_CHECKING, Type

import ops


class CharmOption:
    """Enum representing various configuration options for the charms."""

    def __init__(self, value: str):
        """Initialize a CharmOption with the given value.

        Args:
            value (str): The name of the configuration option.
        """
        self.value = value

    def load(self, charm: ops.CharmBase) -> str | bool | int:
        """Load the value of the configuration option from the charm.

        Args:
            charm (ops.CharmBase): The charm instance from which to load the configuration.

        Returns:
            str | bool | int | float: The value of the configuration option,
            converted to the appropriate type.

        Raises:
            ValueError: If the configuration option is not found in the charmcraft.
        """
        option = charm.meta.config[self.value]
        convert: Type
        if isinstance(self, CharmStrOption):
            convert = str
        elif isinstance(self, CharmBoolOption):
            convert = bool
        elif isinstance(self, CharmIntOption):
            convert = int
        else:
            raise ValueError(f"Unsupported type '{option.type}' for option '{self.value}'.")

        return convert(charm.config[self.value])


class CharmStrOption(CharmOption):
    """Configuration option of type string."""

    if TYPE_CHECKING:  # pragma: no cover

        def load(self: "CharmStrOption", charm: ops.CharmBase) -> str:
            """Type hint for the load method to return a string."""
            ...


class CharmBoolOption(CharmOption):
    """Configuration option of type boolean."""

    if TYPE_CHECKING:  # pragma: no cover

        def load(self: "CharmBoolOption", charm: ops.CharmBase) -> bool:
            """Type hint for the load method to return a boolean."""
            ...


class CharmIntOption(CharmOption):
    """Configuration option of type integer."""

    if TYPE_CHECKING:  # pragma: no cover

        def load(self: "CharmIntOption", charm: ops.CharmBase) -> int:
            """Type hint for the load method to return an integer."""
            ...


DNS_ENABLED = CharmBoolOption("dns-enabled")
DNS_CLUSTER_DOMAIN = CharmStrOption("dns-cluster-domain")
DNS_SERVICE_IP = CharmStrOption("dns-service-ip")
DNS_UPSTREAM_NAMESERVERS = CharmStrOption("dns-upstream-nameservers")
GATEWAY_ENABLED = CharmBoolOption("gateway-enabled")
NETWORK_ENABLED = CharmBoolOption("network-enabled")
INGRESS_ENABLED = CharmBoolOption("ingress-enabled")
INGRESS_ENABLE_PROXY_PROTOCOL = CharmBoolOption("ingress-enable-proxy-protocol")
METRICS_SERVER_ENABLED = CharmBoolOption("metrics-server-enabled")
LOAD_BALANCER_ENABLED = CharmBoolOption("load-balancer-enabled")
LOAD_BALANCER_CIDRS = CharmStrOption("load-balancer-cidrs")
LOAD_BALANCER_L2_MODE = CharmBoolOption("load-balancer-l2-mode")
LOAD_BALANCER_L2_INTERFACES = CharmStrOption("load-balancer-l2-interfaces")
LOAD_BALANCER_BGP_MODE = CharmBoolOption("load-balancer-bgp-mode")
LOAD_BALANCER_BGP_LOCAL_ASN = CharmIntOption("load-balancer-bgp-local-asn")
LOAD_BALANCER_BGP_PEER_ADDRESS = CharmStrOption("load-balancer-bgp-peer-address")
LOAD_BALANCER_BGP_PEER_ASN = CharmIntOption("load-balancer-bgp-peer-asn")
LOAD_BALANCER_BGP_PEER_PORT = CharmIntOption("load-balancer-bgp-peer-port")
LOCAL_STORAGE_ENABLED = CharmBoolOption("local-storage-enabled")
LOCAL_STORAGE_LOCAL_PATH = CharmStrOption("local-storage-local-path")
LOCAL_STORAGE_RECLAIM_POLICY = CharmStrOption("local-storage-reclaim-policy")
