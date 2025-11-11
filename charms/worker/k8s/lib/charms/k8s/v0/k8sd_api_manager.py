# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for managing k8sd API interactions.

This module provides a high-level interface for interacting with K8sd. It
simplifies tasks such as token management and component updates.

The core of the module is the K8sdAPIManager, which handles the creation
and management of HTTP connections to interact with the k8sd API. This
class utilises different connection factories (UnixSocketConnectionFactory
and HTTPConnectionFactory) to establish connections through either Unix
sockets or HTTP protocols.

Example usage for creating a join token for K8sd:

```python
try:
    factory = UnixSocketConnectionFactory('/path/to/socket')
    api_manager = K8sdAPIManager(factory)
    join_token = api_manager.create_join_token('node-name')
except K8sdAPIManagerError as e:
    logger.error("An error occurred: %s", e.message)
```

Similarly, the module allows for requesting authentication tokens and
managing K8s components.
"""

import enum
import json
import logging
import socket
from contextlib import contextmanager
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from typing import Any, Dict, Generator, List, Optional, Tuple, Type, TypeVar, Mapping

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_serializer,
    field_validator,
)

# The unique Charmhub library identifier, never change it
LIBID = "6a5f235306864667a50437c08ba7e83f"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 9

logger = logging.getLogger(__name__)


class ErrorCodes(enum.Enum):
    """Enumerate the response codes from the k8s api.

    Attributes:
        STATUS_NODE_UNAVAILABLE: returned when the node isn't in the cluster
        STATUS_NODE_IN_USE: returned when the node is in the cluster already
    """

    STATUS_NODE_UNAVAILABLE = 520
    STATUS_NODE_IN_USE = 521


class K8sdAPIManagerError(Exception):
    """Base exception for K8sd API Manager errors."""


class K8sdConnectionError(K8sdAPIManagerError):
    """Raised when there is a connection error."""


class InvalidResponseError(K8sdAPIManagerError):
    """Raised when the response is invalid or unexpected.

    Attributes:
        code (int): HTTP Status code
    """

    def __init__(
        self,
        code: int,
        msg: str,
        method: Optional[str] = None,
        endpoint: Optional[str] = None,
        reason: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        """Initialise the InvalidResponseError.

        Args:
            code (int): http response code
            msg (str): Message associated with the error
            method (Optional[str]): The method used to make the request
            endpoint (Optional[str]): The endpoint used to make the request
            reason (Optional[str]): The reason for the error
            body (Optional[str]): The body of the response
        """
        super().__init__(f"Error status {code}\n" + msg)
        self.code = code
        self.method = method
        self.endpoint = endpoint
        self.reason = reason
        self.body = body


class BaseRequestModel(BaseModel):
    """Base model for k8s request responses.

    Attributes:
        type (str): The type of the request.
        status (str): The status of the response, defaults to an empty string.
        status_code (int): The status code of the response.
        operation (str): The operation of the request, defaults to an empty string.
        error_code (int): The error code associated with the response.
        error (str): The error message, defaults to an empty string.
    """

    type: str
    status: str = Field(default="")
    status_code: int
    operation: str = Field(default="")
    error_code: int
    error: str = Field(default="")

    @field_validator("status_code", mode="after")
    def check_status_code(cls, v):
        """Validate the status_code field.

        Args:
            v (int): The value of the status_code field to validate.

        Returns:
            int: The validated status code if it is 200.

        Raises:
            ValueError: If the status_code is not 200.
        """
        if v != 200:
            raise ValueError(f"Status code must be 200. Received {v}")
        return v

    @field_validator("error_code", mode="after")
    def check_error_code(cls, v, info: ValidationInfo):
        """Validate the error_code field.

        Args:
            v (int): The value of the error_code field to validate.
            info (ValidationInfo): The validation information.

        Returns:
            int: The validated error code if it is 0.

        Raises:
            ValueError: If the error_code is not 0.
        """
        if v != 0:
            error_message = info.data.get("error", "Unknown error")
            raise ValueError(f"Error code must be 0, received {v}. Error message: {error_message}")
        return v


class EmptyResponse(BaseRequestModel):
    """Response model for request that do not expect any return value."""


class TokenMetadata(BaseModel):
    """Model representing metadata for a token.

    Attributes:
        token (SecretStr): The token string. (accessible via .get_secret_value() )
    """

    token: SecretStr


class AuthTokenResponse(BaseRequestModel):
    """Response model for Kubernetes authentication token requests.

    Attributes:
        metadata (TokenMetadata): Metadata containing the authentication token.
    """

    metadata: TokenMetadata


class CreateJoinTokenResponse(BaseRequestModel):
    """Response model for join token creation requests.

    Attributes:
        metadata (TokenMetadata): Metadata containing the join token.
    """

    metadata: TokenMetadata


class ClusterMember(BaseModel):
    """Represents a member in the k8sd cluster.

    Attributes:
        name (str): Name of the cluster member.
        address (str): Address of the cluster member.
        cluster_role (str): Cluster Role of the node in the cluster.
        datastore_role (str): Role of the member in the cluster.
    """

    name: str
    address: str
    cluster_role: Optional[str] = Field(default=None, alias="cluster-role")
    datastore_role: Optional[str] = Field(default=None, alias="datastore-role")


class FeatureConfig(BaseModel):
    """Configuration for a specific feature in the cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        enabled: Optional flag which represents the status of the feature.
    """

    model_config = ConfigDict(populate_by_name=True)
    enabled: Optional[bool] = Field(default=None)


class DNSConfig(FeatureConfig):
    """Configuration for the DNS settings of the cluster.

    Attributes:
        cluster_domain: The domain name of the cluster.
        service_ip: The IP address of the DNS service within the cluster.
        upstream_nameservers: List of upstream nameservers for DNS resolution.
    """

    cluster_domain: Optional[str] = Field(default=None, alias="cluster-domain")
    service_ip: Optional[str] = Field(default=None, alias="service-ip")
    upstream_nameservers: Optional[List[str]] = Field(default=None, alias="upstream-nameservers")


class IngressConfig(FeatureConfig):
    """Configuration for the ingress settings of the cluster.

    Attributes:
        enable_proxy_protocol: Optional flag to enable or disable proxy protocol.
    """

    enable_proxy_protocol: Optional[bool] = Field(default=None, alias="enable-proxy-protocol")


class LoadBalancerConfig(FeatureConfig):
    """Configuration for the load balancer settings of the cluster.

    Attributes:
        cidrs: List of CIDR blocks for the load balancer.
        l2_mode: Optional flag to enable or disable layer 2 mode.
        l2_interfaces: List of layer 2 interfaces for the load balancer.
        bgp_mode: Optional flag to enable or disable BGP.
        bgp_local_asn: The local ASN for BGP configuration.
        bgp_peer_address: The peer address for BGP configuration.
        bgp_peer_asn: The peer ASN for BGP configuration.
        bgp_peer_port: The port for BGP peering.
    """

    cidrs: Optional[List[str]] = Field(default=None)
    l2_mode: Optional[bool] = Field(default=None, alias="l2-mode")
    l2_interfaces: Optional[List[str]] = Field(default=None, alias="l2-interfaces")
    bgp_mode: Optional[bool] = Field(default=None, alias="bgp-mode")
    bgp_local_asn: Optional[int] = Field(default=None, alias="bgp-local-asn")
    bgp_peer_address: Optional[str] = Field(default=None, alias="bgp-peer-address")
    bgp_peer_asn: Optional[int] = Field(default=None, alias="bgp-peer-asn")
    bgp_peer_port: Optional[int] = Field(default=None, alias="bgp-peer-port")


class LocalStorageConfig(FeatureConfig):
    """Configuration for the local storage settings of the cluster.

    Attributes:
        local_path: The local path for storage.
        reclaim_policy: The policy for reclaiming local storage.
        set_default: Optional flag to set this as the default storage option.
    """

    local_path: Optional[str] = Field(default=None, alias="local-path")
    reclaim_policy: Optional[str] = Field(default=None, alias="reclaim-policy")
    set_default: Optional[bool] = Field(default=None, alias="set-default")


class NetworkConfig(FeatureConfig):
    """Configuration for the network settings of the cluster."""


class GatewayConfig(FeatureConfig):
    """Configuration for the gateway settings of the cluster."""


class MetricsServerConfig(FeatureConfig):
    """Configuration for the metrics server settings of the cluster."""


class UserFacingClusterConfig(BaseModel):
    """Aggregated configuration model for the user-facing aspects of a cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        network: Network configuration for the cluster.
        dns: DNS configuration for the cluster.
        ingress: Ingress configuration for the cluster.
        load_balancer: Load balancer configuration for the cluster.
        local_storage: Local storage configuration for the cluster.
        gateway: Gateway configuration for the cluster.
        metrics_server: Metrics server configuration for the cluster.
        cloud_provider: The cloud provider for the cluster.
        annotations: Dictionary that can be used to store arbitrary metadata configuration.
    """

    model_config = ConfigDict(populate_by_name=True)

    network: Optional[NetworkConfig] = Field(default=None)
    dns: Optional[DNSConfig] = Field(default=None)
    ingress: Optional[IngressConfig] = Field(default=None)
    load_balancer: Optional[LoadBalancerConfig] = Field(default=None, alias="load-balancer")
    local_storage: Optional[LocalStorageConfig] = Field(default=None, alias="local-storage")
    gateway: Optional[GatewayConfig] = Field(default=None)
    metrics_server: Optional[MetricsServerConfig] = Field(default=None, alias="metrics-server")
    cloud_provider: Optional[str] = Field(default=None, alias="cloud-provider")
    annotations: Optional[Dict[str, str]] = Field(default=None)


class UserFacingDatastoreConfig(BaseModel):
    """Aggregated configuration model for the user-facing datastore aspects of a cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        type: Type of the datastore. For runtime updates, this needs to be "external".
        servers: Server addresses of the external datastore.
        ca_crt: CA certificate of the external datastore cluster in PEM format.
        client_crt: client certificate of the external datastore cluster in PEM format.
        client_key: client key of the external datastore cluster in PEM format.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Optional[str] = Field(default=None)
    servers: Optional[List[str]] = Field(default=None)
    ca_crt: Optional[str] = Field(default=None, alias="ca-crt")
    client_crt: Optional[str] = Field(default=None, alias="client-crt")
    client_key: Optional[str] = Field(default=None, alias="client-key")


class BootstrapConfig(BaseModel):
    """Configuration model for bootstrapping a Canonical K8s cluster.

    Attributes:
        cluster_config (UserFacingClusterConfig): The cluster configuration settings.
        control_plane_taints (List[str]): Register with the following control-plane taints
        pod_cidr (str): The IP address range for the cluster's pods.
        service_cidr (str): The IP address range for the cluster services.
        disable_rbac (bool): Flag to disable role-based access control
        secure_port (int): The secure port used for Kubernetes.
        k8s_dqlite_port (int): The port used by Dqlite.
        datastore_type (str): The type of datastore used by the cluster.
        datastore_servers (List[str]): The servers used by the datastore.
        datastore_ca_cert (str): The CA certificate for the datastore.
        datastore_client_cert (str): The client certificate for accessing the datastore.
        datastore_client_key (str): The client key for accessing the datastore.
        etcd_port (int): The port number for etcd to use.
        etcd_peer_port (int): The port number for etcd peer communication to use.
        etcd_ca_cert (str): The CA certificate to be used for etcd.
        etcd_ca_key (str): The CA key to be used for etcd.
        etcd_server_cert (str): The server certificate to be used for etcd.
        etcd_server_key (str): The server key to be used for etcd.
        etcd_peer_cert (str): The server peer certificate to be used for etcd.
        etcd_peer_key (str): The server peer key to be used for etcd.
        etcd_apiserver_client_cert (str): The client certificate to be used by the kube-apiserver.
        etcd_apiserver_client_key (str): The client key to be used by the kube-apiserver.
        extra_sans (List[str]): List of extra sans for the self-signed certificates
        ca_cert (str): The CA certificate for Kubernetes services.
        ca_key (str): The CA key for Kubernetes services.
        client_ca_cert (str): The client CA certificate for Kubernetes services.
        client_ca_key (str): The client CA key for Kubernetes services.
        front_proxy_ca_cert (str): The front proxy CA certificate.
        front_proxy_ca_key (str): The front proxy CA key.
        front_proxy_client_cert (str): The front proxy client certificate.
        front_proxy_client_key (str): The front proxy client key.
        api_server_kubelet_client_cert (str): The kubelet client certificate for the API server.
        api_server_kubelet_client_key (str): The kubelet client key for the API server.
        admin_client_cert (str): The admin client certificate.
        admin_client_key (str): The admin client key.
        kube_proxy_client_cert (str): The kube-proxy client certificate.
        kube_proxy_client_key (str): The kube-proxy client key.
        kube_scheduler_client_cert (str): The kube-scheduler client certificate.
        kube_scheduler_client_key (str): The kube-scheduler client key.
        kube_controller_manager_client_cert (str): The controller manager client certificate.
        kube_controller_manager_client_key (str): The controller manager client key.
        service_account_key (str): The service account key.
        api_server_cert (str): The API server certificate.
        api_server_key (str): The API server key.
        kubelet_cert (str): The kubelet certificate.
        kubelet_key (str): The kubelet key.
        kubelet_client_cert (str): The kubelet client certificate.
        kubelet_client_key (str): The kubelet client key.
        extra_node_config_files (Dict[str, str]): Additional configuration files for nodes.
        extra_node_kube_apiserver_args (Dict[str, Optional[str]]): key-value
            service args .
        extra_node_kube_controller_manager_args (Dict[str, Optional[str]]):
            key-value service args .
        extra_node_kube_scheduler_args (Dict[str, Optional[str]]): key-value
            service args .
        extra_node_kube_proxy_args (Dict[str, Optional[str]]): key-value
            service args .
        extra_node_kubelet_args (Dict[str, Optional[str]]): key-value service
            args .
        extra_node_containerd_args (Dict[str, Optional[str]]): key-value
            service args .
        extra_node_k8s_dqlite_args (Dict[str, Optional[str]]): key-value
            service args
        extra_node_etcd_args (Dict[str, Optional[str]]): key-value
            service args
        extra_node_containerd_config (Dict[str, Any]): key-value config args
        containerd_base_dir (str): The base directory for containerd.
    """

    cluster_config: Optional[UserFacingClusterConfig] = Field(default=None, alias="cluster-config")
    control_plane_taints: Optional[List[str]] = Field(default=None, alias="control-plane-taints")
    pod_cidr: Optional[str] = Field(default=None, alias="pod-cidr")
    service_cidr: Optional[str] = Field(default=None, alias="service-cidr")
    disable_rbac: Optional[bool] = Field(default=None, alias="disable-rbac")
    secure_port: Optional[int] = Field(default=None, alias="secure-port")
    k8s_dqlite_port: Optional[int] = Field(default=None, alias="k8s-dqlite-port")
    datastore_type: Optional[str] = Field(default=None, alias="datastore-type")
    datastore_servers: Optional[List[str]] = Field(default=None, alias="datastore-servers")
    datastore_ca_cert: Optional[str] = Field(default=None, alias="datastore-ca-crt")
    datastore_client_cert: Optional[str] = Field(default=None, alias="datastore-client-crt")
    datastore_client_key: Optional[str] = Field(default=None, alias="datastore-client-key")
    etcd_port: Optional[int] = Field(default=None, alias="etcd-port")
    etcd_peer_port: Optional[int] = Field(default=None, alias="etcd-peer-port")
    etcd_ca_cert: Optional[str] = Field(default=None, alias="etcd-ca-crt")
    etcd_ca_key: Optional[str] = Field(default=None, alias="etcd-ca-key")
    etcd_server_cert: Optional[str] = Field(default=None, alias="etcd-server-crt")
    etcd_server_key: Optional[str] = Field(default=None, alias="etcd-server-key")
    etcd_peer_cert: Optional[str] = Field(default=None, alias="etcd-peer-crt")
    etcd_peer_key: Optional[str] = Field(default=None, alias="etcd-peer-key")
    etcd_apiserver_client_cert: Optional[str] = Field(
        default=None, alias="etcd-apiserver-client-crt"
    )
    etcd_apiserver_client_key: Optional[str] = Field(
        default=None, alias="etcd-apiserver-client-key"
    )
    extra_sans: Optional[List[str]] = Field(default=None, alias="extra-sans")
    # Cluster-wide external certificates
    ca_cert: Optional[str] = Field(default=None, alias="ca-crt")
    ca_key: Optional[str] = Field(default=None, alias="ca-key")
    client_ca_cert: Optional[str] = Field(default=None, alias="client-ca-crt")
    client_ca_key: Optional[str] = Field(default=None, alias="client-ca-key")
    front_proxy_ca_cert: Optional[str] = Field(default=None, alias="front-proxy-ca-crt")
    front_proxy_ca_key: Optional[str] = Field(default=None, alias="front-proxy-ca-key")
    front_proxy_client_cert: Optional[str] = Field(default=None, alias="front-proxy-client-crt")
    front_proxy_client_key: Optional[str] = Field(default=None, alias="front-proxy-client-key")
    api_server_kubelet_client_cert: Optional[str] = Field(
        default=None, alias="apiserver-kubelet-client-crt"
    )
    api_server_kubelet_client_key: Optional[str] = Field(
        default=None, alias="apiserver-kubelet-client-key"
    )
    admin_client_cert: Optional[str] = Field(default=None, alias="admin-client-crt")
    admin_client_key: Optional[str] = Field(default=None, alias="admin-client-key")
    kube_proxy_client_cert: Optional[str] = Field(default=None, alias="kube-proxy-client-crt")
    kube_proxy_client_key: Optional[str] = Field(default=None, alias="kube-proxy-client-key")
    kube_scheduler_client_cert: Optional[str] = Field(
        default=None, alias="kube-scheduler-client-crt"
    )
    kube_scheduler_client_key: Optional[str] = Field(
        default=None, alias="kube-scheduler-client-key"
    )
    kube_controller_manager_client_cert: Optional[str] = Field(
        default=None, alias="kube-controller-manager-client-crt"
    )
    kube_controller_manager_client_key: Optional[str] = Field(
        default=None, alias="kube-controller-manager-client-key"
    )
    service_account_key: Optional[str] = Field(default=None, alias="service-account-key")
    # Node-specific external certificates
    api_server_cert: Optional[str] = Field(default=None, alias="apiserver-crt")
    api_server_key: Optional[str] = Field(default=None, alias="apiserver-key")
    kubelet_cert: Optional[str] = Field(default=None, alias="kubelet-crt")
    kubelet_key: Optional[str] = Field(default=None, alias="kubelet-key")
    kubelet_client_cert: Optional[str] = Field(default=None, alias="kubelet-client-crt")
    kubelet_client_key: Optional[str] = Field(default=None, alias="kubelet-client-key")
    # Extra configuration files
    extra_node_config_files: Optional[Dict[str, str]] = Field(
        default=None, alias="extra-node-config-files"
    )
    # Extra service arguments (values can be None to delete)
    extra_node_kube_apiserver_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-apiserver-args"
    )
    extra_node_kube_controller_manager_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-controller-manager-args"
    )
    extra_node_kube_scheduler_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-scheduler-args"
    )
    extra_node_kube_proxy_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-proxy-args"
    )
    extra_node_kubelet_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kubelet-args"
    )
    extra_node_containerd_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-containerd-args"
    )
    extra_node_k8s_dqlite_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-k8s-dqlite-args"
    )
    extra_node_etcd_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-etcd-args"
    )
    extra_node_containerd_config: Optional[Mapping[str, Any]] = Field(
        default=None, alias="extra-node-containerd-config"
    )
    containerd_base_dir: Optional[str] = Field(default=None, alias="containerd-base-dir")


class CreateClusterRequest(BaseModel):
    """Request model for creating a new Canonical Kubernetes cluster.

    Attributes:
        name (str): The name of the cluster to be created.
        address (str): The address where the cluster is hosted.
        config (BootstrapConfig): Additional configuration parameters for the cluster.
    """

    name: str
    address: str
    config: BootstrapConfig


class UpdateClusterConfigRequest(BaseModel):
    """Request model for updating Cluster config.

    Attributes:
        config (Optional[UserFacingClusterConfig]): The cluster configuration.
        datastore (Optional[UserFacingDatastoreConfig]): The clusters datastore configuration.
    """

    config: Optional[UserFacingClusterConfig] = Field(default=None)
    datastore: Optional[UserFacingDatastoreConfig] = Field(default=None)


class NodeJoinConfig(BaseModel):
    """Request model for the config on a node joining the cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        kubelet_cert (str): node's certificate
        kubelet_key (str): node's certificate key
        kubelet_client_cert (str): Kubelet client certificate
        kubelet_client_key (str): Kubelet client key
        kube_proxy_client_cert (str): Kube-proxy client certificate
        kube_proxy_client_key (str): Kube-proxy client key
        extra_node_kube_proxy_args (Dict[str, str]): key-value service args
        extra_node_kubelet_args (Dict[str, str]): key-value service args
        extra_node_containerd_args ([Dict[str,str]]): key-value service args
        extra_node_containerd_config ([Dict[str,Any]]): key-value config args
        containerd_base_dir (str): The base directory for containerd.

    """

    model_config = ConfigDict(populate_by_name=True)

    kubelet_cert: Optional[str] = Field(default=None, alias="kubelet-crt")
    kubelet_key: Optional[str] = Field(default=None, alias="kubelet-key")
    kubelet_client_cert: Optional[str] = Field(default=None, alias="kubelet-client-crt")
    kubelet_client_key: Optional[str] = Field(default=None, alias="kubelet-client-key")
    kube_proxy_client_cert: Optional[str] = Field(default=None, alias="kube-proxy-client-crt")
    kube_proxy_client_key: Optional[str] = Field(default=None, alias="kube-proxy-client-key")
    extra_node_kube_proxy_args: Optional[Dict[str, str]] = Field(
        default=None, alias="extra-node-kube-proxy-args"
    )
    extra_node_kubelet_args: Optional[Dict[str, str]] = Field(
        default=None, alias="extra-node-kubelet-args"
    )
    extra_node_containerd_args: Optional[Dict[str, str]] = Field(
        default=None, alias="extra-node-containerd-args"
    )
    extra_node_containerd_config: Optional[Dict[str, Any]] = Field(
        default=None, alias="extra-node-containerd-config"
    )
    containerd_base_dir: Optional[str] = Field(default=None, alias="containerd-base-dir")


class ControlPlaneNodeJoinConfig(NodeJoinConfig):
    """Request model for the config on a control-plane node joining the cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        etcd_server_cert (str): The server certificate to be used for etcd.
        etcd_server_key (str): The server key to be used for etcd.
        etcd_peer_cert (str): The server peer certificate to be used for etcd.
        etcd_peer_key (str): The server peer key to be used for etcd.
        extra_sans (List[str]): List of extra sans for the self-signed certificates
        apiserver_crt (str): API server certificate
        apiserver_key (str): API server certificate key
        front_proxy_client_crt (str): Front proxy client certificate
        front_proxy_client_key (str): Front proxy client key
        admin_client_cert (str): Admin client certificate
        admin_client_key (str): Admin client key
        kube_scheduler_client_cert (str): Kube-scheduler client certificate
        kube_scheduler_client_key (str): Kube-scheduler client key
        kube_controller_manager_client_cert (str): Controller manager client certificate
        kube_controller_manager_client_key (str): Controller manager client key
        extra_node_config_files (Dict[str, str]): Additional node config files
        extra_node_kube_apiserver_args (Dict[str, Optional[str]]): API server args .
        extra_node_kube_controller_manager_args (Dict[str, Optional[str]]): Controller manager args
        extra_node_kube_scheduler_args (Dict[str, Optional[str]]): Scheduler args
        extra_node_k8s_dqlite_args (Dict[str, Optional[str]]): Dqlite args
        extra_node_etcd_args (Dict[str, Optional[str]]): ETCD args
        extra_node_containerd_config (Dict[str, Any]): Containerd config
    """

    model_config = ConfigDict(populate_by_name=True)

    etcd_server_cert: Optional[str] = Field(default=None, alias="etcd-server-crt")
    etcd_server_key: Optional[str] = Field(default=None, alias="etcd-server-key")
    etcd_peer_cert: Optional[str] = Field(default=None, alias="etcd-peer-crt")
    etcd_peer_key: Optional[str] = Field(default=None, alias="etcd-peer-key")
    extra_sans: Optional[List[str]] = Field(default=None, alias="extra-sans")
    apiserver_crt: Optional[str] = Field(default=None, alias="apiserver-crt")
    apiserver_key: Optional[str] = Field(default=None, alias="apiserver-key")
    front_proxy_client_crt: Optional[str] = Field(default=None, alias="front-proxy-client-crt")
    front_proxy_client_key: Optional[str] = Field(default=None, alias="front-proxy-client-key")
    admin_client_cert: Optional[str] = Field(default=None, alias="admin-client-crt")
    admin_client_key: Optional[str] = Field(default=None, alias="admin-client-key")
    kube_scheduler_client_cert: Optional[str] = Field(
        default=None, alias="kube-scheduler-client-crt"
    )
    kube_scheduler_client_key: Optional[str] = Field(
        default=None, alias="kube-scheduler-client-key"
    )
    kube_controller_manager_client_cert: Optional[str] = Field(
        default=None, alias="kube-controller-manager-client-crt"
    )
    kube_controller_manager_client_key: Optional[str] = Field(
        default=None, alias="kube-controller-manager-client-key"
    )
    extra_node_config_files: Optional[Dict[str, str]] = Field(
        default=None, alias="extra-node-config-files"
    )
    extra_node_kube_apiserver_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-apiserver-args"
    )
    extra_node_kube_controller_manager_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-controller-manager-args"
    )
    extra_node_kube_scheduler_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-kube-scheduler-args"
    )
    extra_node_k8s_dqlite_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-k8s-dqlite-args"
    )
    extra_node_etcd_args: Optional[Mapping[str, Optional[str]]] = Field(
        default=None, alias="extra-node-etcd-args"
    )
    extra_node_containerd_config: Optional[Dict[str, Any]] = Field(
        default=None, alias="extra-node-containerd-config"
    )


class JoinClusterRequest(BaseModel):
    """Request model for a node joining the cluster.

    Attributes:
        model_config: ConfigDict instance for the model.
        name (str): node's certificate
        address (str): node's certificate key
        token (str): token
        config (NodeJoinConfig): Node Config
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    address: str
    token: SecretStr
    config: Optional[NodeJoinConfig] = Field(default=None)

    @field_serializer("token")
    def serialize_token(self, token: SecretStr) -> str:
        """Serialize the token to a string.

        Args:
            token (SecretStr): The token to serialize.

        Returns:
            str: The serialized token.
        """
        return token.get_secret_value()

    @field_serializer("config")
    def serialize_config(self, config: Optional[NodeJoinConfig]) -> Optional[str]:
        """Serialize the config to a YAML string.

        Args:
            config (Optional[NodeJoinConfig]): The config to serialize.

        Returns:
            Optional[str]: The serialized config.
        """
        if config is not None:
            return yaml.safe_dump(config.model_dump(exclude_none=True, mode="json", by_alias=True))
        return None


class DatastoreStatus(BaseModel):
    """information regarding the active datastore.

    Attributes:
        datastore_type (str): external or k8s-dqlite datastore
        servers: (List(str)): list of server addresses of the external datastore cluster.
    """

    datastore_type: Optional[str] = Field(default=None, alias="type")
    servers: Optional[List[str]] = Field(default=None, alias="servers")


class FeatureStatus(BaseModel):
    """Represents the status of a feature in the k8sd cluster.

    Attributes:
        enabled (bool):
            shows whether or not the deployment of manifests for a status
            was successful.
        version (str): shows the version of the deployed feature.
        message (str):
            contains information about the status of a feature.
            It is only supposed to be human readable and informative and should
            not be programmatically parsed.
        updated_at (datetime): shows when the last update was done.
    """

    enabled: bool = Field(default=False)
    version: str = Field(default="")
    message: str = Field(default="")
    updated_at: datetime = Field(default_factory=datetime.now, alias="updated-at")


class ClusterStatus(BaseModel):
    """Represents the overall status of the k8sd cluster.

    Attributes:
        ready (bool): Indicates if the cluster is ready.
        members (List[ClusterMember]): List of members in the cluster.
        config (UserFacingClusterConfig): information about the cluster configuration.
        datastore (DatastoreStatus): information regarding the active datastore.
        dns (FeatureStatus): Status of the DNS feature.
        ingress (FeatureStatus): Status of the Ingress feature.
        load_balancer (FeatureStatus): Status of the Load Balancer feature.
        local_storage (FeatureStatus): Status of the Local Storage feature.
        gateway (FeatureStatus): Status of the Gateway feature.
        metrics_server (FeatureStatus): Status of the Metrics Server feature.
        network (FeatureStatus): Status of the Network feature.
        feature_statuses (Iterable[FeatureStatus]): An iterable of all feature statuses.
    """

    ready: bool = Field(False)
    members: Optional[List[ClusterMember]] = Field(default=None)
    config: Optional[UserFacingClusterConfig] = Field(default=None)
    datastore: Optional[DatastoreStatus] = Field(default=None)
    dns: Optional[FeatureStatus] = Field(default=None)
    ingress: Optional[FeatureStatus] = Field(default=None)
    load_balancer: Optional[FeatureStatus] = Field(default=None, alias="load-balancer")
    local_storage: Optional[FeatureStatus] = Field(default=None, alias="local-storage")
    gateway: Optional[FeatureStatus] = Field(default=None)
    metrics_server: Optional[FeatureStatus] = Field(default=None, alias="metrics-server")
    network: Optional[FeatureStatus] = Field(default=None)

    @property
    def by_feature(self) -> List[Tuple[str, Optional[FeatureConfig], Optional[FeatureStatus]]]:
        """Cluster features and their config + status.

        Returns:
            List[Tuple[str, Optional[FeatureConfig], Optional[FeatureStatus]]]:
               A list of tuples where each tuple contains the feature name,
               its config, and its status.
        """
        c = self.config
        return [
            ("dns", c.dns if c else None, self.dns),
            ("ingress", c.ingress if c else None, self.ingress),
            ("load-balancer", c.load_balancer if c else None, self.load_balancer),
            ("local-storage", c.local_storage if c else None, self.local_storage),
            ("gateway", c.gateway if c else None, self.gateway),
            ("metrics-server", c.metrics_server if c else None, self.metrics_server),
            ("network", c.network if c else None, self.network),
        ]


class ClusterMetadata(BaseModel):
    """Metadata containing status information about the k8sd cluster.

    Attributes:
        status (ClusterStatus): The status of the k8sd cluster.
    """

    status: ClusterStatus


class GetClusterStatusResponse(BaseRequestModel):
    """Response model for getting the status of the k8sd cluster.

    Attributes:
        metadata (ClusterMetadata): Metadata containing the cluster status.
                                    Can be None if the status is not available.
    """

    metadata: Optional[ClusterMetadata] = Field(default=None)


class KubeConfigMetadata(BaseModel):
    """Metadata containing kubeconfig.

    Attributes:
        kubeconfig (KubeConfigMetadata): The status of the k8sd cluster.
    """

    kubeconfig: str


class GetKubeConfigResponse(BaseRequestModel):
    """Response model for getting the kubeconfig from the cluster.

    Attributes:
        metadata (KubeconfigMetadata): Metadata containing the kubeconfig.
    """

    metadata: KubeConfigMetadata


class RefreshCertificatesPlanMetadata(BaseModel):
    """Metadata for the certificates plan response.

    Attributes:
        model_config: ConfigDict instance for the model.
        seed (int): The seed for the new certificates.
        certificate_signing_requests (Optional[list[str]]): List of names
        of the CertificateSigningRequests that need to be signed externally (for worker nodes).
    """

    model_config = ConfigDict(populate_by_name=True)

    # NOTE(Hue): Alias is because of a naming mismatch:
    # https://github.com/canonical/k8s-snap-api/blob/6d4139295b37800fb2b3fcce9fc260e6caf284b9/api/v1/rpc_refresh_certificates_plan.go#L12
    seed: Optional[int] = Field(default=None, alias="seconds")
    certificate_signing_requests: Optional[List[str]] = Field(
        default=None, alias="certificate-signing-requests"
    )


class RefreshCertificatesPlanResponse(BaseRequestModel):
    """Response model for the refresh certificates plan.

    Attributes:
        metadata (RefreshCertificatesPlanMetadata): Metadata for the certificates plan response.
    """

    metadata: RefreshCertificatesPlanMetadata


class RefreshCertificatesRunRequest(BaseModel):
    """Request model for running the refresh certificates run.

    Attributes:
        model_config: ConfigDict instance for the model.
        seed (int): The seed for the new certificates from plan response.
        expiration_seconds (int): The duration of the new certificates.
        extra_sans (list[str]): List of extra sans for the new certificates.
    """

    model_config = ConfigDict(populate_by_name=True)

    seed: int
    expiration_seconds: int = Field(alias="expiration-seconds")
    extra_sans: Optional[List[str]] = Field(alias="extra-sans")


class RefreshCertificatesRunMetadata(BaseModel):
    """Metadata for RefreshCertificatesRunResponse.

    Attributes:
        model_config: ConfigDict instance for the model.
        expiration_seconds (int): The duration of the new certificates
        (might not match the requested value).
    """

    model_config = ConfigDict(populate_by_name=True)

    expiration_seconds: int = Field(alias="expiration-seconds")


class RefreshCertificatesRunResponse(BaseRequestModel):
    """Response model for the refresh certificates run.

    Attributes:
        metadata (RefreshCertificatesRunMetadata): Metadata for the certificates run response.
    """

    metadata: RefreshCertificatesRunMetadata


class GetClusterConfigMetadata(BaseModel):
    """Metadata containing the cluster config.

    Attributes:
        status (UserFacingClusterConfig): The configuration of the cluster.
        datastore (UserFacingDatastoreConfig): The configuration of the datastore.
        pod_cidr (str): The CIDR range for the pods in the cluster.
        service_cidr (str): The CIDR range for the services in the cluster.
    """

    model_config = ConfigDict(populate_by_name=True)

    status: UserFacingClusterConfig
    datastore: Optional[UserFacingDatastoreConfig] = Field(default=None)
    pod_cidr: Optional[str] = Field(default=None, alias="pod-cidr")
    service_cidr: Optional[str] = Field(default=None, alias="service-cidr")


class GetClusterConfigResponse(BaseRequestModel):
    """Response model for the get cluster config endpoint.

    Attributes:
        metadata (GetClusterConfigMetadata): Metadata containing the cluster config.
    """

    metadata: GetClusterConfigMetadata


class NodeStatus(BaseModel):
    """Represents the status of a node in the cluster.

    Attributes:
        name (Optional[str]): The name of the node.
        address (Optional[str]): The address of the node.
        cluster_role (Optional[str]): The role of the node in the cluster.
        datastore_role (Optional[str]): The role of the node in the datastore cluster.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)
    cluster_role: Optional[str] = Field(default=None, alias="cluster-role")
    datastore_role: Optional[str] = Field(default=None, alias="datastore-role")


class GetNodeStatusMetadata(BaseModel):
    """Metadata containing the node status.

    Attributes:
        status (NodeStatus): The status of the local node.
        taints (Optional[List[str]]): List of taints applied to the node.
    """

    status: NodeStatus
    taints: Optional[List[str]] = Field(default=None)


class GetNodeStatusResponse(BaseRequestModel):
    """Response model for the get node status endpoint.

    Attributes:
        metadata (GetNodeStatusMetadata): Metadata containing the node status.
    """

    metadata: GetNodeStatusMetadata


T = TypeVar("T", bound=BaseRequestModel)


class UnixSocketHTTPConnection(HTTPConnection):
    """HTTP connection over a Unix socket."""

    def __init__(self, unix_socket: str, timeout: int = 30):
        """Initialise the UnixSocketHTTPConnection.

        Args:
            unix_socket (str): Path to the Unix socket.
            timeout (int): Connection timeout in seconds.
        """
        super().__init__("localhost", timeout=timeout)
        self.unix_socket = unix_socket

    def connect(self):
        """Establish a connection to the server using a Unix socket.

        Raises:
            K8sdConnectionError: If there is an error connecting to the Unix socket.
        """
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.unix_socket)
        except socket.error as e:
            raise K8sdConnectionError(f"Error connecting to socket: {self.unix_socket}") from e


class ConnectionFactory:
    """Abstract factory for creating connection objects."""

    @contextmanager
    def create_connection(self) -> Generator[HTTPConnection, None, None]:
        """Create a new connection instance.

        Raises:
            NotImplementedError: If create_connection is not implemented by the subclass.
        """
        raise NotImplementedError("create_connection must be implemented by subclasses")


class UnixSocketConnectionFactory(ConnectionFactory):
    """Concrete factory for creating Unix socket connections."""

    def __init__(self, unix_socket: str, timeout: int = 30):
        """Initialize a new instance of UnixSocketConnectionFactory.

        Args:
            unix_socket (str): The file path to the Unix socket.
            timeout (int, optional): The timeout for the connection in seconds.
                                     Defaults to 30 seconds.
        """
        self.unix_socket = unix_socket
        self.timeout = timeout

    @contextmanager
    def create_connection(self):
        """Create and manage a Unix socket HTTP connection.

        Yields:
            UnixSocketHTTPConnection: The created Unix socket HTTP connection.
        """
        conn = UnixSocketHTTPConnection(self.unix_socket, self.timeout)
        try:
            yield conn
        finally:
            conn.close()


class HTTPConnectionFactory(ConnectionFactory):
    """Concrete factory for creating HTTP connections."""

    def __init__(self, host: str, port=None, timeout: int = 30):
        """Initialize a new instance of HTTPConnectionFactory.

        Args:
            host (str): Hostname for the HTTP connection.
            port (int): Port for the HTTP connection.
            timeout (int, optional): The timeout for the connection in seconds.
                                     Defaults to 30 seconds.
        """
        self.host = host
        self.port = port
        self.timeout = timeout

    @contextmanager
    def create_connection(self):
        """Create and manage an HTTP connection.

        Yields:
            HTTPConnection: The created HTTP connection.
        """
        conn = HTTPConnection(self.host, self.port, self.timeout)
        try:
            yield conn
        finally:
            conn.close()


class K8sdAPIManager:
    """Manager for K8sd API interactions."""

    def __init__(self, factory: ConnectionFactory):
        """Initialise the K8sdAPIManager.

        Args:
            factory (ConnectionFactory): An instance of a connection factory that will be used
                                         to create connections. This factory determines the type
                                         of connection (e.g., Unix socket or HTTP).
        """
        self.factory = factory

    def _send_request(
        self, endpoint: str, method: str, response_cls: Type[T], body: Optional[dict] = None
    ) -> T:
        """Send a request to the k8sd API endpoint.

        Args:
            endpoint (str): The endpoint to send the request to.
            method (str): HTTP method for the request.
            body (dict): Body of the request.
            response_cls (Type[T]): The response class to deserialize the response into.

        Raises:
            K8sdConnectionError: If there's an HTTP or socket error.
            InvalidResponseError: If the response has invalid JSON or fails validation.

        Returns:
            T: An instance of the response class with the response data.
        """
        try:
            with self.factory.create_connection() as connection:
                body_data = json.dumps(body) if body is not None else None
                headers = {"Content-Type": "application/json"} if body_data is not None else {}

                connection.request(
                    method,
                    endpoint,
                    body=body_data,
                    headers=headers,
                )
                response = connection.getresponse()
                data = response.read().decode()
                if not 200 <= response.status < 300:
                    raise InvalidResponseError(
                        response.status,
                        f"\tmethod={method}\n"
                        f"\tendpoint={endpoint}\n"
                        f"\treason={response.reason}\n"
                        f"\tbody={data}",
                        method=method,
                        endpoint=endpoint,
                        reason=response.reason,
                        body=data,
                    )
            return response_cls.parse_raw(data)

        except ValueError as e:
            raise InvalidResponseError(
                response.status,
                f"Request failed:\n\tmethod={method}\n\tendpoint={endpoint}",
            ) from e
        except (socket.error, HTTPException) as e:
            raise K8sdConnectionError(
                f"HTTP or Socket error\tmethod={method}\n\tendpoint={endpoint}"
            ) from e

    def create_join_token(self, name: str, worker: bool = False) -> SecretStr:
        """Create a join token.

        Args:
            name (str): Name of the node.
            worker (bool): Whether the node should join as control-plane or worker.

        Returns:
            SecretStr: The generated join token if successful.
        """
        endpoint = "/1.0/k8sd/cluster/tokens"
        body = {
            "name": name,
            "worker": worker,
        }
        join_response = self._send_request(endpoint, "POST", CreateJoinTokenResponse, body)
        return join_response.metadata.token

    def join_cluster(self, config: JoinClusterRequest):
        """Join a node to the k8s cluster.

        Args:
            config: JoinClusterRequest: config to join the cluster
        """
        endpoint = "/1.0/k8sd/cluster/join"
        request = config.model_dump(
            exclude_none=True, by_alias=True, mode="json", exclude_unset=True
        )
        self._send_request(endpoint, "POST", EmptyResponse, request)

    def remove_node(self, name: str, force: bool = False):
        """Remove a node from the cluster.

        Args:
            name (str): Name of the node that should be removed.
            force (bool): Forcibly remove the node
        """
        endpoint = "/1.0/k8sd/cluster/remove"
        body = {"name": name, "force": force}
        self._send_request(endpoint, "POST", EmptyResponse, body)

    def update_cluster_config(self, config: UpdateClusterConfigRequest):
        """Enable or disable a k8s component.

        Args:
            config (UpdateClusterConfigRequest): The cluster configuration.
        """
        endpoint = "/1.0/k8sd/cluster/config"
        body = config.model_dump(exclude_none=True, by_alias=True)
        self._send_request(endpoint, "PUT", EmptyResponse, body)

    def get_cluster_config(self) -> GetClusterConfigResponse:
        """Retrieve the cluster configuration.

        Worker nodes are not allowed to call this endpoint:
        https://github.com/canonical/k8s-snap/blob/0a5edd2/src/k8s/pkg/k8sd/api/endpoints.go#L121-L126

        Returns:
            GetClusterConfigResponse: The cluster configuration.
        """
        return self._send_request("/1.0/k8sd/cluster/config", "GET", GetClusterConfigResponse)

    def get_node_status(self) -> GetNodeStatusResponse:
        """Retrieve the status of the local node.

        Returns:
            GetNodeStatusResponse: The status of the local node.
        """
        return self._send_request("/1.0/k8sd/node", "GET", GetNodeStatusResponse)

    def get_cluster_status(self) -> GetClusterStatusResponse:
        """Retrieve cluster status.

        Returns:
            cluster_status: status of the cluster.
        """
        return self._send_request("/1.0/k8sd/cluster", "GET", GetClusterStatusResponse)

    def is_cluster_bootstrapped(self) -> bool:
        """Check if K8sd has been bootstrapped.

        Returns:
            bool: True if the cluster has been bootstrapped, False otherwise.
        """
        try:
            status = self.get_cluster_status()
            return status.error_code == 0
        except (K8sdConnectionError, InvalidResponseError) as e:
            logger.error("Invalid response while checking if cluster is bootstrapped: %s", e)
        return False

    def is_cluster_ready(self):
        """Check if the Kubernetes cluster is ready.

        The cluster is ready if at least one k8s node is in READY state.

        Returns:
            bool: True if the cluster is ready, False otherwise.
        """
        status = self.get_cluster_status()
        return status.metadata and status.metadata.status.ready

    def check_k8sd_ready(self):
        """Check if k8sd is ready using various microcluster endpoints.

        Raises:
            K8sdConnectionError: If the response is Not Found on all endpoints.
        """
        ready_endpoints = ["/core/1.0/ready", "/cluster/1.0/ready"]
        for i, endpoint in enumerate(ready_endpoints):
            try:
                self._send_request(endpoint, "GET", EmptyResponse)
                break
            except InvalidResponseError as ex:
                if ex.code == 404:
                    logger.warning(
                        "micro-cluster unavailable @ %s (%s of %s): %s",
                        endpoint,
                        i + 1,
                        len(ready_endpoints),
                        ex,
                    )
                    # Try the next endpoint if the current one is not found
                    continue
                raise
        else:
            raise K8sdConnectionError(
                "Exhausted all endpoints while checking if micro-cluster is ready"
            )

    def bootstrap_k8s_snap(self, request: CreateClusterRequest) -> None:
        """Bootstrap the k8s cluster.

        Args:
            request (CreateClusterRequest): The request model to bootstrap the cluster.
        """
        endpoint = "/1.0/k8sd/cluster"
        body = request.dict(exclude_none=True, by_alias=True)
        self._send_request(endpoint, "POST", EmptyResponse, body)

    def request_auth_token(self, username: str, groups: List[str]) -> SecretStr:
        """Request a Kubernetes authentication token.

        Args:
            username (str): Username for which the token is requested.
            groups (List[str]): Groups associated with the user.

        Returns:
            SecretStr: The authentication token.
        """
        endpoint = "/1.0/kubernetes/auth/tokens"
        body = {"username": username, "groups": groups}
        auth_response = self._send_request(endpoint, "POST", AuthTokenResponse, body)
        return auth_response.metadata.token

    def revoke_auth_token(self, token: str) -> None:
        """Revoke a Kubernetes authentication token.

        Args:
            token (str): The authentication token.
        """
        endpoint = "/1.0/kubernetes/auth/tokens"
        body = {"token": token}
        self._send_request(endpoint, "DELETE", EmptyResponse, body)

    def get_kubeconfig(self, server: Optional[str]) -> str:
        """Request a Kubernetes admin config.

        Args:
            server (str): Optional server to replace in the kubeconfig endpoint

        Returns:
            str: The authentication token.
        """
        endpoint = "/1.0/k8sd/kubeconfig"
        body = {"server": server or ""}
        response = self._send_request(endpoint, "GET", GetKubeConfigResponse, body)
        return response.metadata.kubeconfig

    def refresh_certs(
        self, extra_sans: List[str], expiration_seconds: Optional[int] = None
    ) -> None:
        """Refresh the certificates for the cluster.

        Args:
            extra_sans (list[str]): List of extra SANs for the certificates.
            expiration_seconds (Optional[int]): The duration of the new certificates.
        """
        plan_endpoint = "/1.0/k8sd/refresh-certs/plan"
        plan_resp = self._send_request(plan_endpoint, "POST", RefreshCertificatesPlanResponse, {})

        # NOTE(Hue): Default certificate expiration is set to 20 years:
        # https://github.com/canonical/k8s-snap/blob/32e35128394c0880bcc4ce87447f4247cc315ba5/src/k8s/pkg/k8sd/app/hooks_bootstrap.go#L331-L338
        if expiration_seconds is None:
            now = datetime.now()
            twenty_years_later = datetime(
                now.year + 20, now.month, now.day, now.hour, now.minute, now.second
            )
            expiration_seconds = int((twenty_years_later - now).total_seconds())

        run_endpoint = "/1.0/k8sd/refresh-certs/run"
        run_req = RefreshCertificatesRunRequest(  # type: ignore
            seed=plan_resp.metadata.seed,
            expiration_seconds=expiration_seconds,
            extra_sans=extra_sans,
        )

        body = run_req.dict(exclude_none=True, by_alias=True)
        self._send_request(run_endpoint, "POST", RefreshCertificatesRunResponse, body)
