# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing
"""Mocks for unit tests."""

from typing import Optional, TypedDict


class MockELBRequest:
    """Mock ELB request."""

    class Protocols:
        """Mock Protocols.

        Attributes:
            tcp: tcp
            https: https
        """

        tcp = "tcp"
        https = "https"

    def __init__(self, protocols: Protocols):
        """Initialize ELB request.

        Args:
            protocols: request protocols mock
        """

        class HealthCheck(TypedDict):
            """Health check.

            Attributes:
                protocol: the used protocol
                port: the port to health check
                path: the path to health check
            """

            protocol: str
            port: int
            path: str

        self.name = ""
        self.protocol = ""
        self.protocols = protocols
        self.port_mapping: dict[int, int] = {}
        self.public = False
        self.health_checks: list[HealthCheck] = []

    def add_health_check(self, protocol: str, port: int, path: str):
        """Add health check.

        Args:
            protocol: the used protocol
            port: the port to health check
            path: the path to health check
        """
        self.health_checks.append({"protocol": protocol, "port": port, "path": path})


class MockELBResponse:
    """Mock ELB response."""

    def __init__(self, addr: str):
        """Initialize ELB response.

        Args:
            addr: the lb address
        """
        self.error: str = ""
        self.address: str = addr


class MockEvent:
    """Mock event."""

    class Params:
        """Mock params."""

        def __init__(self, kv: Optional[dict] = None):
            """Initialize params.

            Args:
                kv: key-value pairs
            """
            self.kv = kv if kv else {}

        def get(self, key: str):
            """Get value.

            Args:
                key: the key to get

            Returns:
                the value for the key or "X_default_value"
            """
            return self.kv.get(key, "X_default_value")

    def __init__(self, params: Params):
        """Initialize event.

        Args:
            params: event parameters mock
        """
        self.params = params
        self.failed = False
        self.failed_msg = ""
        self.results = {}  # type: ignore

    def fail(self, msg: str):
        """Fail event.

        Args:
            msg: the failure message
        """
        self.failed = True
        self.failed_msg = msg

    def set_results(self, results: dict):
        """Set results.

        Args:
            results: the results
        """
        self.results = results
