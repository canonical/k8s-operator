#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Snap Installation Module."""


import logging
import subprocess
from pathlib import Path
from typing import List, Literal, Optional, Union

import charms.operator_libs_linux.v2.snap as snap_lib
import yaml
from pydantic import BaseModel, Field, ValidationError, parse_obj_as
from typing_extensions import Annotated

# Log messages can be retrieved using juju debug-log
log = logging.getLogger(__name__)


class SnapFileArgument(BaseModel):
    """Structure to install a snap by file.

    Attributes:
        install_type (str): literal string defining this type
        name (str): The name of the snap after installed
        filename (Path): Path to the snap to locally install
        classic (bool): If it should be installed as a classic snap
        dangerous (bool): If it should be installed as a dangerouse snap
        devmode (bool): If it should be installed as with dev mode enabled
    """

    install_type: Literal["file"] = Field("file", alias="install-type", exclude=True)
    name: str = Field(exclude=True)
    filename: Optional[Path] = None
    classic: Optional[bool] = None
    devmode: Optional[bool] = None
    dangerous: Optional[bool] = None


class SnapStoreArgument(BaseModel):
    """Structure to install a snap by snapstore.

    Attributes:
        install_type (str): literal string defining this type
        name (str): The type of the request.
        state (SnapState): a `SnapState` to reconcile to.
        classic (bool): If it should be installed as a classic snap
        devmode (bool): If it should be installed as with dev mode enabled
        channel (bool): the channel to install from
        cohort (str): the key of a cohort that this snap belongs to
        revision (int): the revision of the snap to install
    """

    install_type: Literal["store"] = Field("store", alias="install-type", exclude=True)
    name: str = Field(exclude=True)
    classic: Optional[bool] = None
    devmode: Optional[bool] = None
    state: Optional[snap_lib.SnapState] = Field(snap_lib.SnapState.Present)
    channel: Optional[str] = None
    cohort: Optional[str] = None
    revision: Optional[int] = None


SnapArgument = Annotated[
    Union[SnapFileArgument, SnapStoreArgument], Field(discriminator="install_type")
]


def _parse_management_arguments() -> List[SnapArgument]:
    """Parse snap management arguments.

    Raises:
        SnapError: when the management issue cannot be resolved

    Returns:
        Parsed arguments list for the specific host architecture
    """
    revision = Path("templates/snap_installation.yaml")
    if not revision.exists():
        raise snap_lib.SnapError(f"Failed to find file={revision}")
    try:
        with revision.open() as f:
            body = yaml.safe_load(f)
    except yaml.YAMLError as e:
        log.error("Failed to load file=%s, %s", revision, e)
        raise snap_lib.SnapError(f"Failed to load file={revision}")
    dpkg_arch = ["dpkg", "--print-architecture"]
    arch = subprocess.check_output(dpkg_arch).decode("UTF-8").strip()

    if not (isinstance(body, dict) and (arch_spec := body.get(arch))):
        log.warning("Failed to find revision for arch=%s", arch)
        raise snap_lib.SnapError(f"Failed to find revision for arch={arch}")

    try:
        args: List[SnapArgument] = [parse_obj_as(SnapArgument, arg) for arg in arch_spec]  # type: ignore[arg-type]
    except ValidationError as e:
        log.warning("Failed to validate args=%s (%s)", arch_spec, e)
        raise snap_lib.SnapError("Failed to validate snap args")

    return args


def management():
    """Manage snap installations on this machine."""
    cache = snap_lib.SnapCache()
    for args in _parse_management_arguments():
        which = cache[args.name]
        if isinstance(args, SnapFileArgument) and which.revision != "x1":
            snap_lib.install_local(**args.dict(exclude_none=True))
        elif isinstance(args, SnapStoreArgument):
            log.info("Ensuring %s snap version", args.name)
            which.ensure(**args.dict(exclude_none=True))
