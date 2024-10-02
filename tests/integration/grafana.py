# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aid for connecting to grafana instance."""

import base64
import json
import logging
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class Grafana:
    """A class to manage interactions with a running instance of Grafana."""

    def __init__(
        self,
        model_name: str,
        base: Optional[str] = "http://localhost",
        username: Optional[str] = "admin",
        password: Optional[str] = "",
    ):
        """Initialize Grafana instance.

        Args:
            model_name (str): The name of the model where Grafana is deployed.
            base (Optional[str]): Base url of Grafana application. Defaults to 'http://localhost'.
            username (Optional[str]): Username for authentication. Defaults to 'admin'.
            password (Optional [str]): Password for authentication. Defaults to ''.
        """
        self.base_uri = f"{base}/{model_name}-grafana"
        self.username = username
        self.password = password

    def _get_with_auth(self, url: str) -> str:
        """Send GET request with basic authentication.

        Args:
            url (str): The URL to send the request to.

        Returns:
            str: The response data.
        """
        log.info("Query: %s", url)
        credentials = f"{self.username}:{self.password}"
        encoded_creds = base64.b64encode(credentials.encode("ascii"))
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Basic {encoded_creds.decode('ascii')}")

        with urllib.request.urlopen(request) as response:
            assert response.code == 200, f"Failed to get endpoint {url}"
            data = response.read().decode()
        return data

    async def is_ready(self) -> bool:
        """Check if Grafana is ready.

        Returns:
            bool: True if Grafana is ready, False otherwise.
        """
        res = await self.health()
        return res.get("database", "") == "ok" or False

    async def health(self) -> Dict:
        """Query the API to check Grafana's health.

        Returns:
            dict: A dictionary containing basic API health information.
        """
        uri = f"{self.base_uri}/api/health"

        data = self._get_with_auth(uri)

        return json.loads(data)

    async def dashboards_all(self) -> List:
        """Retrieve all dashboards.

        Returns:
            list: Found dashboards, if any.
        """
        uri = f"{self.base_uri}/api/search?starred=false"

        data = self._get_with_auth(uri)

        return json.loads(data)
