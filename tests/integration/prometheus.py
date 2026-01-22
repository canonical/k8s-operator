# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Aid for testing promtheus."""

import json
import logging
import urllib.parse
import urllib.request
from typing import List, Optional

log = logging.getLogger(__name__)


class Prometheus:
    """A class for managing interactions with a running instance of Prometheus."""

    def __init__(
        self,
        model_name: str,
        base: Optional[str] = "http://localhost",
    ):
        """Initialize Prometheus instance.

        Args:
            model_name (str): The name of the model where Prometheus is deployed.
            base (Optional[str]): Base url of the Prometheus application.
                Defaults to 'http://localhost'.
        """
        self.base_uri = f"{base}/{model_name}-prometheus-0"

    def _get_url(self, url):
        """Send GET request to the provided URL.

        Args:
            url (str): The URL to send the request to.

        Returns:
            str: The response data.
        """
        log.info("Query: %s", url)
        with urllib.request.urlopen(url) as response:
            data = response.read().decode()

        assert response.code == 200, f"Failed to get endpoint {url}: {data}"
        return data

    async def is_ready(self) -> bool:
        """Check if Prometheus is ready.

        Returns:
            bool: True if Prometheus is ready, False otherwise.
        """
        res = await self.health()
        return "Prometheus Server is Ready." in res

    async def health(self) -> str:
        """Check Prometheus readiness using the MGMT API.

        Returns:
            str: A string containing "Prometheus is Ready" if it is ready;
                otherwise, an empty string.
        """
        api_path = "-/ready"
        uri = f"{self.base_uri}/{api_path}"

        data = self._get_url(uri)

        return data

    async def get_metrics(self, query: str) -> List:
        """Query Prometheus for metrics.

        Args:
            query (str): The Prometheus query to execute.

        Returns:
            List: A list of results from the query.
        """
        api_path = "api/v1/query"
        uri = f"{self.base_uri}/{api_path}"

        encoded_query = urllib.parse.urlencode({"query": query}).encode("ascii")
        request = urllib.request.Request(uri, data=encoded_query)
        response = urllib.request.urlopen(request)
        data = response.read().decode()

        assert response.code == 200, f"Failed to query '{query}': {data}"
        result = json.loads(data)
        assert result.get("status") == "success", f"Query failed: {result}"
        return result.get("data", {}).get("result", [])
