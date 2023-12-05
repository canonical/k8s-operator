#!/usr/bin/env python3

# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests."""

import asyncio
import logging
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text(encoding="utf-8"))
APP_NAME = METADATA["name"]


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, pytestconfig: pytest.Config):
    """Deploy the charm together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # Deploy the charm and wait for active/idle status
    if charm := pytestconfig.getoption("--charm-file"):
        charm = Path(charm)
        log.info("Specific charm-file %s...", charm)
    if not charm:
        log.info("Search for charm...")
        charm = next(Path.cwd().glob("*.charm"), None)
    if not charm:
        log.info("Build charm...")
        charm = await ops_test.build_charm(".")
    assert ops_test.model
    await asyncio.gather(
        ops_test.model.deploy(charm.absolute(), application_name=APP_NAME, series="jammy"),
        ops_test.model.wait_for_idle(
            apps=[APP_NAME], status="active", raise_on_blocked=True, timeout=1000
        ),
    )
