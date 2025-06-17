# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=duplicate-code,missing-function-docstring
"""Unit tests for config.option module."""

import unittest.mock as mock

import config.option
import ops
import pytest


def test_charm_option_load():
    """Test that CharmOption.load() returns the correct type based on the option type."""
    charm = mock.MagicMock(spec=ops.CharmBase)
    charm.meta.config = {
        "test_str": ops.ConfigMeta("test_str", "string", None, None),
        "test_bool": ops.ConfigMeta("test_bool", "boolean", None, None),
        "test_int": ops.ConfigMeta("test_int", "int", None, None),
    }
    charm.config = {
        "test_str": "default_value",
        "test_bool": True,
        "test_int": 42,
    }

    str_option = config.option.CharmStrOption("test_str")
    assert str_option.load(charm) == "default_value"

    bool_option = config.option.CharmBoolOption("test_bool")
    assert bool_option.load(charm) is True

    int_option = config.option.CharmIntOption("test_int")
    assert int_option.load(charm) == 42


def test_charm_option_load_missing_option():
    """Test that CharmOption.load() returns the correct type based on the option type."""
    charm = mock.MagicMock(spec=ops.CharmBase)
    charm.meta.config = {}
    str_option = config.option.CharmStrOption("missing_str")
    with pytest.raises(ValueError):
        # Missing option should raise ValueError
        str_option.load(charm)


def test_charm_option_load_incorrect_type():
    """Test that CharmOption.load() returns the correct type based on the option type."""
    charm = mock.MagicMock(spec=ops.CharmBase)
    charm.meta.config = {
        "test_str": ops.ConfigMeta("test_str", "string", None, None),
    }
    bool_option = config.option.CharmBoolOption("test_str")
    with pytest.raises(TypeError):
        # Missing option should raise ValueError
        bool_option.load(charm)
