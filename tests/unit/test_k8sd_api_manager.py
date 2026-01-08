# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
# Ignore Pylint requiring docstring for each test function.
# pylint: disable=C0116
"""Unit tests for K8sdAPIManager."""

import socket
import unittest
from socket import AF_UNIX, SOCK_STREAM
from typing import Union
from unittest.mock import MagicMock, call, patch

from k8sd_api_manager import (
    AuthTokenResponse,
    BaseRequestModel,
    BootstrapConfig,
    ControlPlaneNodeJoinConfig,
    CreateClusterRequest,
    CreateJoinTokenResponse,
    DNSConfig,
    EmptyResponse,
    InvalidResponseError,
    JoinClusterRequest,
    K8sdAPIManager,
    K8sdConnectionError,
    LocalStorageConfig,
    NetworkConfig,
    RefreshCertificatesPlanMetadata,
    RefreshCertificatesPlanResponse,
    RefreshCertificatesRunRequest,
    RefreshCertificatesRunResponse,
    TokenMetadata,
    UnixSocketHTTPConnection,
    UpdateClusterConfigRequest,
    UserFacingClusterConfig,
    UserFacingDatastoreConfig,
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
                getattr(model, key),
                value,
                f"Model attribute {key} did not match expected value",
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
        assert "Status code must be 200" in str(context.exception)

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
        assert "Error code must be 0" in str(context.exception)


class TestBootstrapConfigTyping(unittest.TestCase):
    """Test BootstrapConfig types."""

    def test_json_representation_drops_unset_fields(self):
        """Test a default BootstrapConfig is empty."""
        config = BootstrapConfig()
        assert config.model_dump_json(exclude_none=True, by_alias=True) == "{}"

    def test_json_representation_coerced_from_str(self):
        """Test a field that should be an int, is parsed from a str."""
        config = BootstrapConfig(**{"k8s-dqlite-port": "1"})
        assert config.k8s_dqlite_port == 1
        assert config.model_dump_json(exclude_none=True, by_alias=True) == '{"k8s-dqlite-port":1}'


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
        assert "Error connecting to socket" in str(context.exception)


class TestK8sdAPIManager(unittest.TestCase):
    """Test K8sdAPIManager."""

    def setUp(self):
        """Set up environment."""
        self.mock_factory = MagicMock()
        self.api_manager = K8sdAPIManager(factory=self.mock_factory)

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_check_k8sd_in_error(self, mock_send_request):
        """Test bootstrap."""
        not_found = InvalidResponseError(code=404, msg="Not Found")
        in_error = InvalidResponseError(code=504, msg="In Error")
        mock_send_request.side_effect = [not_found, in_error]

        with self.assertRaises(InvalidResponseError) as ie:
            self.api_manager.check_k8sd_ready()
        mock_send_request.assert_has_calls(
            [
                call("/core/1.0/ready", "GET", EmptyResponse),
                call("/cluster/1.0/ready", "GET", EmptyResponse),
            ]
        )
        assert ie.exception.code == 504

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_check_k8sd_not_found(self, mock_send_request):
        """Test bootstrap."""
        not_found = InvalidResponseError(code=404, msg="Not Found")
        mock_send_request.side_effect = [not_found, not_found]

        with self.assertRaises(K8sdConnectionError):
            self.api_manager.check_k8sd_ready()

        mock_send_request.assert_has_calls(
            [
                call("/core/1.0/ready", "GET", EmptyResponse),
                call("/cluster/1.0/ready", "GET", EmptyResponse),
            ]
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_check_k8sd_ready(self, mock_send_request):
        """Test bootstrap."""
        not_found = InvalidResponseError(code=404, msg="Not Found")
        success = EmptyResponse(status_code=200, type="test", error_code=0)
        mock_send_request.side_effect = [not_found, success]

        self.api_manager.check_k8sd_ready()

        mock_send_request.assert_has_calls(
            [
                call("/core/1.0/ready", "GET", EmptyResponse),
                call("/cluster/1.0/ready", "GET", EmptyResponse),
            ]
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
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

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_create_join_token(self, mock_send_request):
        """Test successful request for join token."""
        mock_send_request.return_value = CreateJoinTokenResponse(
            status_code=200,
            type="test",
            error_code=0,
            metadata=TokenMetadata(token="test-token"),
        )

        self.api_manager.create_join_token("test-node")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/tokens",
            "POST",
            CreateJoinTokenResponse,
            {"name": "test-node", "worker": False},
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_create_join_token_worker(self, mock_send_request):
        """Test successful request for join token for a worker."""
        mock_send_request.return_value = CreateJoinTokenResponse(
            status_code=200,
            type="test",
            error_code=0,
            metadata=TokenMetadata(token="test-token"),
        )

        self.api_manager.create_join_token("test-node", worker=True)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/tokens",
            "POST",
            CreateJoinTokenResponse,
            {"name": "test-node", "worker": True},
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_join_cluster_control_plane(self, mock_send_request):
        """Test successfully joining a cluster."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        request = JoinClusterRequest(
            name="test-node", address="127.0.0.1:6400", token="test-token"
        )
        request.config = ControlPlaneNodeJoinConfig(extra_sans=["127.0.0.1"])
        self.api_manager.join_cluster(request)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/join",
            "POST",
            EmptyResponse,
            {
                "name": "test-node",
                "address": "127.0.0.1:6400",
                "token": "test-token",
                "config": "extra-sans:\n- 127.0.0.1\n",
            },
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_join_cluster_worker(self, mock_send_request):
        """Test successfully joining a cluster."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        request = JoinClusterRequest(
            name="test-node", address="127.0.0.1:6400", token="test-token"
        )
        self.api_manager.join_cluster(request)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/join",
            "POST",
            EmptyResponse,
            {"name": "test-node", "address": "127.0.0.1:6400", "token": "test-token"},
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_remove_node(self, mock_send_request):
        """Test successfully removing a node from the cluster."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        self.api_manager.remove_node("test-node")
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/remove",
            "POST",
            EmptyResponse,
            {"name": "test-node", "force": False},
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_update_cluster_config(self, mock_send_request):
        """Test successfully updating cluster config."""
        mock_send_request.return_value = EmptyResponse(status_code=200, type="test", error_code=0)

        dns_config = DNSConfig(enabled=True)
        local_storage_config = LocalStorageConfig(enabled=True)
        user_config = UserFacingClusterConfig(dns=dns_config, local_storage=local_storage_config)
        datastore = UserFacingDatastoreConfig(
            type="external",
            servers=["localhost:123"],
            ca_crt="ca-crt",
            client_crt="client-crt",
            client_key="client-key",
        )
        request = UpdateClusterConfigRequest(config=user_config, datastore=datastore)
        self.api_manager.update_cluster_config(request)
        mock_send_request.assert_called_once_with(
            "/1.0/k8sd/cluster/config",
            "PUT",
            EmptyResponse,
            {
                "config": {
                    "dns": {"enabled": True},
                    "local-storage": {"enabled": True},
                },
                "datastore": {
                    "type": "external",
                    "servers": ["localhost:123"],
                    "ca-crt": "ca-crt",
                    "client-crt": "client-crt",
                    "client-key": "client-key",
                },
            },
        )

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_request_auth_token(self, mock_send_request):
        """Test successfully requesting auth-token."""
        test_token = "foo:mytoken"
        mock_send_request.return_value = AuthTokenResponse(
            status_code=200,
            type="test",
            error_code=0,
            metadata=TokenMetadata(token=test_token),
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

    @patch("k8sd_api_manager.K8sdAPIManager._send_request")
    def test_refresh_certs(self, mock_send_request: MagicMock):
        """Test successfully calling refresh certificates endpoints on K8sd.

        Args:
            mock_send_request: the mocked send_request function
        """
        extra_sans = ["test-sans1", "test-sans2", "1.2.3.4"]
        expiration_seconds = 180
        seed = 123
        plan_args = (
            "/1.0/k8sd/refresh-certs/plan",
            "POST",
            RefreshCertificatesPlanResponse,
            {},
        )  # type: ignore
        run_req = RefreshCertificatesRunRequest(  # type: ignore
            seed=seed,
            expiration_seconds=expiration_seconds,  # type: ignore
            extra_sans=extra_sans,  # type: ignore
        )
        run_body = run_req.dict(exclude_none=True, by_alias=True)
        run_args = (
            "/1.0/k8sd/refresh-certs/run",
            "POST",
            RefreshCertificatesRunResponse,
            run_body,
        )  # type: ignore

        def mock_send_request_se(
            *args,
        ) -> Union[RefreshCertificatesPlanResponse, EmptyResponse]:
            """Mock send_request side effect.

            Args:
                args: the arguments to the call

            Returns:
                the response for the call
            """
            if args == plan_args:
                return RefreshCertificatesPlanResponse(
                    status_code=200,
                    type="test",
                    error_code=0,
                    metadata=RefreshCertificatesPlanMetadata(seconds=seed),
                )
            if args == run_args:
                return EmptyResponse(status_code=200, type="test", error_code=0)

            return EmptyResponse(status_code=404, type="test", error_code=1)

        mock_send_request.side_effect = mock_send_request_se
        self.api_manager.refresh_certs(extra_sans, expiration_seconds)
        mock_send_request.assert_has_calls([call(*plan_args), call(*run_args)], any_order=False)
