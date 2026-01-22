# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Accessor for charm config options in the right type."""

from typing import TYPE_CHECKING, Type

import ops


class CharmOption:
    """Enum representing various configuration options for the charms."""

    def __init__(self, name: str):
        """Initialize a CharmOption with the given name.

        Args:
            name (str): The name of the configuration option.
        """
        self.name = name

    def get(self, charm: ops.CharmBase) -> str | bool | int:
        """Get the value of the configuration option from the charm.

        Args:
            charm (ops.CharmBase): The charm instance from which to load the configuration.

        Returns:
            str | bool | int : The value of the configuration option,
            converted to the appropriate type.

        Raises:
            ValueError: If the charm does not have the configuration option.
            TypeError: If the configuration option type doesn't match charmcraft.yaml
        """
        option = charm.meta.config.get(self.name)
        if option is None:
            raise ValueError(f"Unsupported configuration option '{self.name}'.")
        convert: Type
        if option.type == "string" and isinstance(self, StrOption):
            convert = str
        elif option.type == "boolean" and isinstance(self, BoolOption):
            convert = bool
        elif option.type == "int" and isinstance(self, IntOption):
            convert = int
        else:
            raise TypeError(f"Unsupported type '{option.type}' for option '{self.name}'.")

        return convert(charm.config[self.name])


class StrOption(CharmOption):
    """Configuration option of type string."""

    if TYPE_CHECKING:  # pragma: no cover

        def get(self: "StrOption", charm: ops.CharmBase) -> str:
            """Type hint for the get method to return a string."""
            ...


class BoolOption(CharmOption):
    """Configuration option of type boolean."""

    if TYPE_CHECKING:  # pragma: no cover

        def get(self: "BoolOption", charm: ops.CharmBase) -> bool:
            """Type hint for the get method to return a boolean."""
            ...


class IntOption(CharmOption):
    """Configuration option of type integer."""

    if TYPE_CHECKING:  # pragma: no cover

        def get(self: "IntOption", charm: ops.CharmBase) -> int:
            """Type hint for the get method to return an integer."""
            ...
