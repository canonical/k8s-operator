# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Additions to tools missing from juju library."""

import logging
from typing import Optional

from juju.model import Model

logger = logging.getLogger(__name__)


async def get_address(model: Model, app_name: str, unit_num: Optional[int] = None) -> str:
    """Find unit address for any application.

    Args:
        model: juju model
        app_name: string name of application
        unit_num: integer number of a juju unit

    Returns:
        unit address as a string
    """
    status = await model.get_status()
    app = status["applications"][app_name]
    return (
        app.public_address
        if unit_num is None
        else app["units"][f"{app_name}/{unit_num}"]["address"]
    )
