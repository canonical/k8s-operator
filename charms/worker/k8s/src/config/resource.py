# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""A module for detecting changes in Juju resources."""

import logging
from hashlib import sha256
from pathlib import Path

import ops

log = logging.getLogger(__name__)
JUJU_RESOURCE_DIR = Path.cwd().parent / "resources"


class CharmResource:
    """A class to manage Juju resources for a charm."""

    def __init__(self, charm: ops.CharmBase, name: str) -> None:
        self._charm = charm
        self.metadata = charm.meta.resources[name]
        self.original_hash = (
            sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        )

    @property
    def path(self) -> Path:
        """Get the path to the resource file."""
        if not self.metadata.filename:
            raise ValueError("Resource does not have a filename defined in metadata.")
        return JUJU_RESOURCE_DIR / self.metadata.filename

    def fetch(self) -> Path:
        """Fetch the resource from Juju.

        Returns:
            Path: The path to the fetched resource.
        """
        try:
            resource_path = self._charm.model.resources.fetch(self.metadata.resource_name)
        except (ops.ModelError, NameError):
            log.error(
                "Something went wrong when claiming '%s' resource.", self.metadata.resource_name
            )
            resource_path = self.path
        return resource_path

    @property
    def current_hash(self) -> str | None:
        """Get the current hash of the resource.

        Returns:
            str: The current hash of the resource.
        """
        current = self.fetch()
        return sha256(current.read_bytes()).hexdigest() if current.exists() else None

    @property
    def is_updated(self) -> bool:
        """Check if the resource has been updated."""
        if diff := self.current_hash != self.original_hash:
            log.info(
                "Resource '%s' has been updated. Original hash: %s, Current hash: %s",
                self.metadata.resource_name,
                self.original_hash,
                self.current_hash,
            )
        else:
            log.info("Resource '%s' is unchanged.", self.metadata.resource_name)
        return diff
