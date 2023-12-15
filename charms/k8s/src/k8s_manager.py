# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for managing Kubernetes Snap interactions."""
import json
import socket
from http.client import HTTPConnection, HTTPException
from typing import List, Type, TypeVar

from pydantic import BaseModel, Field, ValidationError

K8SD_SNAP_SOCKET = "/var/snap/k8s/common/var/lib/k8sd/control.socket"


class K8sSnapManagerError(Exception):
    """Base exception for K8s Snap Manager errors."""


class K8sConnectionError(K8sSnapManagerError):
    """Raised when there is a connection error."""


class InvalidResponseError(K8sSnapManagerError):
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

    def is_successful(self) -> bool:
        """Check if the request was successful.

        Returns:
            bool: True if the request was successful, False otherwise.
        """
        return self.status == "Success" and self.status_code == 200 and self.error_code == 0


class UpdateComponentResponse(BaseRequestModel):
    """Response model for updating a k8s component."""


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
        metadata (TokenMetadata): Metadata containing the newly created join token.
    """

    metadata: TokenMetadata


T = TypeVar("T", bound=BaseRequestModel)


class UnixSocketHTTPConnection(HTTPConnection):
    """HTTP connection over a Unix socket."""

    def __init__(self, unix_socket, timeout=30):
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
            K8sConnectionError: If there is an error connecting to the Unix socket.
        """
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.unix_socket)
        except socket.error as e:
            raise K8sConnectionError(f"Error connecting to socket: {e}") from e


class K8sSnapManager:
    """Manager for Kubernetes Snap interactions."""

    def __init__(self, use_unix_socket=False, host="localhost", port=6400):
        """Initialise the K8sSnapManager.

        Args:
            use_unix_socket (bool): Determines if Unix socket should be used for connection.
            host (str): Hostname for the HTTP connection.
            port (int): Port for the HTTP connection.
        """
        self.use_unix_socket = use_unix_socket
        self.host = host
        self.port = port

    def _send_request(self, endpoint: str, method: str, body: dict, response_cls: Type[T]) -> T:
        """Send a request to the k8sd API endpoint.

        Args:
            endpoint (str): The endpoint to send the request to.
            method (str): HTTP method for the request.
            body (dict): Body of the request.
            response_cls (Type[T]): The response class to deserialize the response into.

        Raises:
            K8sConnectionError: If there's an HTTP or socket error.
            InvalidResponseError: If the response has invalid JSON or fails validation.

        Returns:
            T: An instance of the response class with the response data.
        """
        try:
            conn = (
                UnixSocketHTTPConnection(K8SD_SNAP_SOCKET)
                if self.use_unix_socket
                else HTTPConnection(self.host, self.port)
            )
            conn.request(
                method,
                endpoint,
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            response_data = json.loads(response.read().decode())
            conn.close()
        except (socket.error, HTTPException) as e:
            raise K8sConnectionError(f"HTTP or Socket error: {e}") from e
        except json.JSONDecodeError as e:
            raise InvalidResponseError("Invalid JSON in response") from e

        try:
            return response_cls(**response_data)
        except ValidationError as e:
            raise InvalidResponseError(f"Response validation failed: {e}") from e

    def create_join_token(self, name: str):
        """Create a join token.

        Args:
            name (str): Name of the node.

        Returns:
            str: The generated join token if successful.
        """
        endpoint = "/1.0/k8sd/tokens"
        body = {"name": name}
        join_response = self._send_request(endpoint, "POST", body, CreateJoinTokenResponse)
        return join_response.metadata.token

    def manage_component(self, name: str, enable: bool):
        """Manage a k8s component.

        Args:
            name (str): Name of the component.
            enable (bool): True to enable, False to disable the component.


        Raises:
            InvalidResponseError: If the response from the component management request
            is unsuccessful or invalid.
        """
        endpoint = f"/1.0/k8sd/components/{name}"
        body = {"status": "enable" if enable else "disable"}
        component_response = self._send_request(endpoint, "PUT", body, UpdateComponentResponse)
        if not component_response.is_successful():
            raise InvalidResponseError(
                f"Failed to {body['status']} {name} component: {component_response.error}"
            )

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
        auth_response = self._send_request(endpoint, "POST", body, AuthTokenResponse)
        return auth_response.metadata.token
