import json
import urllib.parse
import urllib.request
from typing import Optional



class Prometheus:
    """A class for managing interactions with a running instance of Prometheus."""

    def __init__(
        self,
        model_name: str,
        host: Optional[str] = "localhost",
    ):
        """Initialize Prometheus instance.

        Args:
            model_name (str): The name of the model where Prometheus is deployed.
            host (Optional[str]): Host address of the Prometheus application. Defaults to 'localhost'.
        """
        self.base_uri = f"http://{host}/{model_name}-prometheus-0"

    def _get_url(self, url):
        """Send GET request to the provided URL.

        Args:
            url (str): The URL to send the request to.

        Returns:
            str: The response data.

        Raises:
            AssertionError: If the response status code is not 200.
        """
        response = urllib.request.urlopen(url)
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
            str: A string containing "Prometheus is Ready" if it is ready; otherwise, an empty string.
        """
        api_path = "-/ready"
        uri = f"{self.base_uri}/{api_path}"

        data = self._get_url(uri)

        return data

    async def check_metrics(self, query: str):
        """Query Prometheus for metrics.

        Args:
            query (str): The Prometheus query to execute.

        Raises:
            AssertionError: If the query fails or if data is not yet available.
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
        assert result.get("data", {}).get("result"), f"Data not yet available"
