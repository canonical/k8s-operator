# Copyright 2024 Canonical Ltd.
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
import json
import logging
import socket
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
from typing import Generator, List, Optional, Type, TypeVar

from pydantic import BaseModel, Field, validator

# The unique Charmhub library identifier, never change it
LIBID = "6a5f235306864667a50437c08ba7e83f"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2

logger = logging.getLogger(__name__)


class K8sdAPIManagerError(Exception):
    """Base exception for K8sd API Manager errors."""


class K8sdConnectionError(K8sdAPIManagerError):
    """Raised when there is a connection error."""


class InvalidResponseError(K8sdAPIManagerError):
    """Raised when the response is invalid or unexpected."""


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

    @validator("status_code", always=True)
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

    @validator("error_code", always=True)
    def check_error_code(cls, v, values):
        """Validate the error_code field.

        Args:
            v (int): The value of the error_code field to validate.
            values (dict): The values dictionary.

        Returns:
            int: The validated error code if it is 0.

        Raises:
            ValueError: If the error_code is not 0.
        """
        if v != 0:
            error_message = values.get("error", "Unknown error")
            raise ValueError(f"Error code must be 0, received {v}. Error message: {error_message}")
        return v


class EmptyResponse(BaseRequestModel):
    """Response model for request that do not expect any return value."""


class TokenMetadata(BaseModel):
    """Model representing metadata for a token.

    Attributes:
        token (str): The actual token string.
    """

    token: str


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
        Name (str): Name of the cluster member.
        Address (str): Address of the cluster member.
        Role (str): Role of the member in the cluster.
        Fingerprint (str): Fingerprint for the member.
    """

    name: str
    address: str
    cluster_role: str = Field(..., alias="cluster-role")
    datastore_role: str = Field(..., alias="datastore-role")


class ClusterComponent(BaseModel):
    """Represents a component in the k8sd cluster.

    Attributes:
        name (str): Name of the component.
        status (str): Current status of the component.
    """

    name: str
    status: str


class ClusterStatus(BaseModel):
    """Represents the overall status of the k8sd cluster.

    Attributes:
        Ready (bool): Indicates if the cluster is ready.
        Members (List[ClusterMember]): List of members in the cluster.
        Components (List[ClusterComponent]): List of components in the cluster.
    """

    ready: bool
    members: Optional[List[ClusterMember]]
    components: Optional[List[ClusterComponent]]


class ClusterMetadata(BaseModel):
    """Metadata containing status information about the k8sd cluster.

    Attributes:
        status (ClusterStatus): The status of the k8sd cluster.
    """

    status: ClusterStatus


class GetClusterStatusResponse(BaseRequestModel):
    """Response model for getting the status of the k8sd cluster.

    Attributes:
        metadata (Optional[ClusterMetadata]): Metadata containing the cluster status.
                                              Can be None if the status is not available.
    """

    metadata: Optional[ClusterMetadata] = None


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
                        f"Error status {response.status}\n"
                        f"\tmethod={method}\n"
                        f"\tendpoint={endpoint}\n"
                        f"\treason={response.reason}\n"
                        f"\tbody={data}"
                    )
            return response_cls.parse_raw(data)

        except ValueError as e:
            raise InvalidResponseError(
                f"Request failed:\n" f"\tmethod={method}\n" f"\tendpoint={endpoint}"
            ) from e
        except (socket.error, HTTPException) as e:
            raise K8sdConnectionError(
                f"HTTP or Socket error" f"\tmethod={method}\n" f"\tendpoint={endpoint}"
            ) from e

    def create_join_token(self, name: str, worker: bool = False):
        """Create a join token.

        Args:
            name (str): Name of the node.
            worker (bool): Whether the node should join as control-plane or worker.

        Returns:
            str: The generated join token if successful.
        """
        endpoint = "/1.0/k8sd/cluster/tokens"
        body = {
            "name": name,
            "worker": worker,
        }
        join_response = self._send_request(endpoint, "POST", CreateJoinTokenResponse, body)
        return join_response.metadata.token

    def join_cluster(self, name: str, address: str, token: str):
        """Join a node to the k8s cluster.

        Args:
            name (str): Name of the node.
            address (str): address to which k8sd should be bound
            token (str): The join token for this node.
        """
        endpoint = "/1.0/k8sd/cluster/join"
        body = {"name": name, "address": address, "token": token}
        self._send_request(endpoint, "POST", EmptyResponse, body)

    def remove_node(self, name: str, force: bool = True):
        """Remove a node from the cluster.

        Args:
            name (str): Name of the node that should be removed.
            force (bool): Forcibly remove the node
        """
        endpoint = "/1.0/k8sd/cluster/remove"
        body = {"name": name, "force": force}
        self._send_request(endpoint, "POST", EmptyResponse, body)

    def enable_component(self, name: str, enable: bool):
        """Enable or disable a k8s component.

        Args:
            name (str): Name of the component.
            enable (bool): True to enable, False to disable the component.
        """
        endpoint = f"/1.0/k8sd/components/{name}"
        body = {"status": "enabled" if enable else "disabled"}
        self._send_request(endpoint, "PUT", EmptyResponse, body)

    def is_cluster_bootstrapped(self) -> bool:
        """Check if K8sd has been bootstrapped.

        Returns:
            bool: True if the cluster has been bootstrapped, False otherwise.
        """
        try:
            endpoint = "/1.0/k8sd/cluster"
            cluster_status = self._send_request(endpoint, "GET", GetClusterStatusResponse)
            return cluster_status.error_code == 0
        except (K8sdConnectionError, InvalidResponseError) as e:
            logger.error("Invalid response while checking if cluster is bootstrapped: %s", e)
        return False

    def is_cluster_ready(self):
        """Check if the Kubernetes cluster is ready.

        The cluster is ready if at least one k8s node is in READY state.

        Returns:
            bool: True if the cluster is ready, False otherwise.
        """
        endpoint = "/1.0/k8sd/cluster"
        cluster_status = self._send_request(endpoint, "GET", GetClusterStatusResponse)
        if cluster_status.metadata:
            return cluster_status.metadata.status.ready
        return False

    def check_k8sd_ready(self):
        """Check if k8sd is ready."""
        endpoint = "/cluster/1.0/ready"
        self._send_request(endpoint, "GET", EmptyResponse)

    def bootstrap_k8s_snap(self, name: str, address: str) -> None:
        """Bootstrap the k8s cluster.

        Args:
            name (str): name of the node
            address (str): address to which k8sd should be bound

        TODO: Add bootstrap config support
        """
        endpoint = "/cluster/control"
        body = {"bootstrap": True, "name": name, "address": address}
        self._send_request(endpoint, "POST", EmptyResponse, body)

    def request_auth_token(self, username: str, groups: List[str]) -> str:
        """Request a Kubernetes authentication token.

        Args:
            username (str): Username for which the token is requested.
            groups (List[str]): Groups associated with the user.

        Returns:
            str: The authentication token.
        """
        endpoint = "/1.0/kubernetes/auth/tokens"
        body = {"username": username, "groups": groups}
        auth_response = self._send_request(endpoint, "POST", AuthTokenResponse, body)
        return auth_response.metadata.token
