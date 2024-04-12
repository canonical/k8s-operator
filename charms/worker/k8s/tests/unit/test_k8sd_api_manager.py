# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
# Ignore Pylint requiring docstring for each test function.
# pylint: disable=C0116
"""Unit tests for K8sdAPIManager."""

import socket
import unittest
from socket import AF_UNIX, SOCK_STREAM
from unittest.mock import MagicMock, patch

from lib.charms.k8s.v0.k8sd_api_manager import (
    AuthTokenResponse,
    BaseRequestModel,
    BootstrapConfig,
    CreateClusterRequest,
    CreateJoinTokenResponse,
    DNSConfig,
    EmptyResponse,
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
    NetworkConfig,
    TokenMetadata,
    UnixSocketHTTPConnection,
    UpdateClusterConfigRequest,
    UserFacingClusterConfig,
)


class TestBaseRequestModel(unittest.TestCase):
    """Test BaseRequestModel."""

    def test_successful_instantiation(self):
        """Test successfully instantiating a K8sApiManager."""
        valid_data = {
            "type": "test_type",
            "status": "test_status",
            "status_code": 200,
            "operation": "test_operation",
            "error_code": 0,
            "error": "",
        }
        model = BaseRequestModel(**valid_data)
        for key, value in valid_data.items():
            self.assertEqual(
                getattr(model, key), value, f"Model attribute {key} did not match expected value"
            )

    def test_invalid_status_code(self):
        """Test handling invalid status code."""
        invalid_data = {
            "type": "test_type",
            "status": "test_status",
            "status_code": 404,
            "operation": "test_operation",
            "error_code": 0,
            "error": "",
        }
        with self.assertRaises(ValueError) as context:
            BaseRequestModel(**invalid_data)
        self.assertIn("Status code must be 200", str(context.exception))

    def test_invalid_error_code(self):
        """Test handling invalid error code."""
        invalid_data = {
            "type": "test_type",
            "status": "test_status",
            "status_code": 200,
            "operation": "test_operation",
            "error_code": 1,
            "error": "Ruh-roh!",
        }
        with self.assertRaises(ValueError) as context:
            BaseRequestModel(**invalid_data)
        self.assertIn("Error code must be 0", str(context.exception))


class TestBootstrapConfigTyping(unittest.TestCase):
    """Test BootstrapConfig types."""

    def test_json_representation_drops_unset_fields(self):
        """Test a default BootstrapConfig is empty."""
        config = BootstrapConfig()
        assert config.json(exclude_none=True, by_alias=True) == "{}"

    def test_json_representation_coerced_from_str(self):
        """Test a field that should be an int, is parsed from a str."""
        config = BootstrapConfig(**{"k8s-dqlite-port": "1"})
        assert config.k8s_dqlite_port == 1
        assert config.json(exclude_none=True, by_alias=True) == '{"k8s-dqlite-port": 1}'

    def test_json_representation_coerced_from_int(self):
        """Test a field that should be a str, is parsed from an int."""
        config = BootstrapConfig(**{"datastore-type": 1})
        assert config.datastore_type == "1"
        assert config.json(exclude_none=True, by_alias=True) == '{"datastore-type": "1"}'


class TestUnixSocketHTTPConnection(unittest.TestCase):
    """Test UnixSocketHTTPConnection."""

    @patch("socket.socket")
    def test_connection_success(self, mock_socket: MagicMock):
        """Test successful connection."""
        socket_path = "/path/to/socket"
        conn = UnixSocketHTTPConnection(socket_path)

        mock_socket_instance = MagicMock()
        mock_socket.return_value = mock_socket_instance

        conn.connect()

        mock_socket.assert_called_once_with(AF_UNIX, SOCK_STREAM)
        mock_socket_instance.settimeout.assert_called_once_with(conn.timeout)
        mock_socket_instance.connect.assert_called_once_with(socket_path)

    @patch("socket.socket")
    def test_connection_failure(self, mock_socket):
        """Test connection failure."""
        socket_path = "/path/to/socket"
        conn = UnixSocketHTTPConnection(socket_path)

        mock_socket_instance = MagicMock()
        mock_socket.return_value = mock_socket_instance
        mock_socket_instance.connect.side_effect = OSError("Mocked socket error")

        with self.assertRaises(K8sdConnectionError) as context:
            conn.connect()

        mock_socket.assert_called_once_with(AF_UNIX, SOCK_STREAM)
        self.assertIn("Error connecting to socket", str(context.exception))


class TestK8sdAPIManager(unittest.TestCase):
    """Test K8sdAPIManager."""

    def setUp(self):
        """Setup environment."""
        self.mock_factory = MagicMock()
        self.api_manager = K8sdAPIManager(factory=self.mock_factory)

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_bootstrap_k8s_snap(self, mock_send_request):
        """Test bootstrap."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        a = NetworkConfig(enabled=False)
        b = UserFacingClusterConfig(network=a)
        config = BootstrapConfig(**{"cluster-config": b})

        self.api_manager.bootstrap_k8s_snap(
            CreateClusterRequest(name="test-node", address="127.0.0.1:6400", config=config)
        )
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster",
            "POST",
            EmptyResponse,
            {
                "name": "test-node",
                "address": "127.0.0.1:6400",
                "config": {"cluster-config": {"network": {"enabled": False}}},
            },
        )

    def test_create_join_token_invalid_response(self):
        """Test invalid request for join token."""
        mock_connection = MagicMock()
        self.mock_factory.create_connection.return_value.__enter__.return_value = mock_connection
        mock_connection.getresponse.return_value.status = 500
        mock_connection.getresponse.return_value.read.return_value = (
            '{"invalid": "response"}'.encode()
        )

        with self.assertRaises(InvalidResponseError):
            self.api_manager.create_join_token("test-node")

    def test_create_join_token_connection_error(self):
        """Test errored request for join token."""
        self.mock_factory.create_connection.side_effect = socket.error("Connection failed")

        with self.assertRaises(K8sdConnectionError):
            self.api_manager.create_join_token("test-node")

    def test_create_join_token_success(self):
        """Test successful request for join token."""
        mock_connection = MagicMock()
        self.mock_factory.create_connection.return_value.__enter__.return_value = mock_connection
        mock_connection.getresponse.return_value.status = 200
        mock_connection.getresponse.return_value.read.return_value = (
            '{"status_code": 200, "type": "test", \
                "error_code": 0, "metadata":{"token":"test-token"}}'
        ).encode()

        token = self.api_manager.create_join_token("test-node")

        self.assertEqual(token.get_secret_value(), "test-token")
        mock_connection.request.assert_called_once_with(
            "POST",
            "/1.0/k8sd/cluster/tokens",
            body='{"name": "test-node", "worker": false}',
            headers={"Content-Type": "application/json"},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_create_join_token(self, mock_send_request):
        """Test successful request for join token."""
        mock_send_request.return_value = CreateJoinTokenResponse(
            status_code=200, type="test", error_code=0, metadata=TokenMetadata(token="test-token")
        )

        self.api_manager.create_join_token("test-node")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/tokens",
            "POST",
            CreateJoinTokenResponse,
            {"name": "test-node", "worker": False},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_create_join_token_worker(self, mock_send_request):
        """Test successful request for join token for a worker."""
        mock_send_request.return_value = CreateJoinTokenResponse(
            status_code=200, type="test", error_code=0, metadata=TokenMetadata(token="test-token")
        )

        self.api_manager.create_join_token("test-node", worker=True)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/tokens",
            "POST",
            CreateJoinTokenResponse,
            {"name": "test-node", "worker": True},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_join_cluster(self, mock_send_request):
        """Test successfully joining a cluster."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.join_cluster("test-node", "127.0.0.1:6400", "test-token")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/join",
            "POST",
            EmptyResponse,
            {"name": "test-node", "address": "127.0.0.1:6400", "token": "test-token"},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_remove_node(self, mock_send_request):
        """Test successfully removing a node from the cluster."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.remove_node("test-node")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/remove", "POST", EmptyResponse, {"name": "test-node", "force": True}
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_update_cluster_config(self, mock_send_request):
        """Test successfully updating cluster config."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        dns_config = DNSConfig(enabled=True)
        user_config = UserFacingClusterConfig(dns=dns_config)
        request = UpdateClusterConfigRequest(config=user_config)
        self.api_manager.update_cluster_config(request)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/config",
            "PUT",
            EmptyResponse,
            {"config": {"dns": {"enabled": True}}},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_request_auth_token(self, mock_send_request):
        """Test successfully requesting auth-token."""
        test_token = "foo:mytoken"
        mock_send_request.return_value = AuthTokenResponse(
            status_code=200, type="test", error_code=0, metadata=TokenMetadata(token=test_token)
        )

        test_user = "test_user"
        test_groups = ["bar", "baz"]
        token = self.api_manager.request_auth_token(test_user, test_groups)
        assert token.get_secret_value() == test_token
        mock_send_request.assert_called_once_with(
            "/1.0/kubernetes/auth/tokens",
            "POST",
            AuthTokenResponse,
            {"username": test_user, "groups": test_groups},
        )
