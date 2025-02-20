# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Changed configuration handling."""

from typing import List


class ChangedConfig:
    """A class to represent the changed configuration."""

    def __init__(self, name, cluster_config, charm_config):
        """Create a new instance of the ChangedConfig class."""
        self.name = name
        self.cluster_config = cluster_config
        self.charm_config = charm_config

    def __str__(self) -> str:
        """Return a string representation of the ChangedConfig instance."""
        return f"{self.name}: {self.cluster_config=}, {self.charm_config=}"


class BootstrapConfigChangeError(Exception):
    """An exception raised when a bootstrap config is changed."""

    def __init__(self, changed: List[ChangedConfig]):
        self.changed = changed
        self._changed_str = "\n".join(str(c) for c in changed)
        super().__init__(
            "Bootstrap config options can not be changed. "
            f"Change the charm config options to match the cluster config:\n{self._changed_str}"
        )
