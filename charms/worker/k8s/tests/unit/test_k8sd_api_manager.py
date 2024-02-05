# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
# Ignore Pylint requiring docstring for each test function.
# pylint: disable=C0116
"""Unit tests for K8sdAPIManager"""

import socket
import unittest
from socket import AF_UNIX, SOCK_STREAM
from unittest.mock import MagicMock, patch

from lib.charms.k8s.v0.k8sd_api_manager import (
    AuthTokenResponse,
    BaseRequestModel,
    CreateJoinTokenResponse,
    EmptyResponse,
    InvalidResponseError,
    K8sdAPIManager,
    K8sdConnectionError,
    TokenMetadata,
    UnixSocketHTTPConnection,
)


class TestBaseRequestModel(unittest.TestCase):
    """Test BaseRequestModel"""

    def test_successful_instantiation(self):
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


class TestUnixSocketHTTPConnection(unittest.TestCase):
    """Test UnixSocketHTTPConnection"""

    @patch("socket.socket")
    def test_connection_success(self, mock_socket: MagicMock):
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
    """Test K8sdAPIManager"""

    def setUp(self):
        """Setup environment."""
        self.mock_factory = MagicMock()
        self.api_manager = K8sdAPIManager(factory=self.mock_factory)

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_bootstrap_k8s_snap(self, mock_send_request):
        mock_send_request.return_value = EmptyResponse(
            status_code=200, type="test", error_code=0, metadata="foo"
        )

        self.api_manager.bootstrap_k8s_snap("test-node", "127.0.0.1:6400")
        mock_send_request.assert_called_once_with(
            "/cluster/control",
            "POST",
            EmptyResponse,
            {"bootstrap": True, "name": "test-node", "address": "127.0.0.1:6400"},
        )

    def test_create_join_token_invalid_response(self):
        mock_connection = MagicMock()
        self.mock_factory.create_connection.return_value.__enter__.return_value = mock_connection
        mock_connection.getresponse.return_value.status = 500
        mock_connection.getresponse.return_value.read.return_value = (
            '{"invalid": "response"}'.encode()
        )

        with self.assertRaises(InvalidResponseError):
            self.api_manager.create_join_token("test-node")

    def test_create_join_token_connection_error(self):
        self.mock_factory.create_connection.side_effect = socket.error("Connection failed")

        with self.assertRaises(K8sdConnectionError):
            self.api_manager.create_join_token("test-node")

    def test_create_join_token_success(self):
        mock_connection = MagicMock()
        self.mock_factory.create_connection.return_value.__enter__.return_value = mock_connection
        mock_connection.getresponse.return_value.status = 200
        mock_connection.getresponse.return_value.read.return_value = (
            '{"status_code": 200, "type": "test", \
                "error_code": 0, "metadata":{"token":"test-token"}}'
        ).encode()

        token = self.api_manager.create_join_token("test-node")

        self.assertEqual(token, "test-token")
        mock_connection.request.assert_called_once_with(
            "POST",
            "/1.0/k8sd/cluster/tokens",
            body='{"name": "test-node", "worker": false}',
            headers={"Content-Type": "application/json"},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_create_join_token(self, mock_send_request):
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
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.remove_node("test-node")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/test-node",
            "DELETE",
            EmptyResponse,
            None
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_enable_component__enable(self, mock_send_request):
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.enable_component("foo", True)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/components/foo",
            "PUT",
            EmptyResponse,
            {"status": "enabled"},
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_enable_component__disable(self, mock_send_request):
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.enable_component("foo", False)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/components/foo", "PUT", EmptyResponse, {"status": "disabled"}
        )

    @patch("lib.charms.k8s.v0.k8sd_api_manager.K8sdAPIManager._send_request")
    def test_request_auth_token(self, mock_send_request):
        test_token = "foo:mytoken"
        mock_send_request.return_value = AuthTokenResponse(
            status_code=200, type="test", error_code=0, metadata=TokenMetadata(token=test_token)
        )

        test_user = "test_user"
        test_groups = ["bar", "baz"]
        token = self.api_manager.request_auth_token(test_user, test_groups)
        assert token == test_token
        mock_send_request.assert_called_once_with(
            "/1.0/kubernetes/auth/tokens",
            "POST",
            AuthTokenResponse,
            {"username": test_user, "groups": test_groups},
        )
