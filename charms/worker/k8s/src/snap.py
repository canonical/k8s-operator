#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Snap Installation Module."""


import logging
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union

import charms.operator_libs_linux.v2.snap as snap_lib
import ops
import yaml
from pydantic import BaseModel, Field, ValidationError, parse_obj_as, validator
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
        dangerous (bool): If it should be installed as a dangerous snap
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
        revision (str): the revision of the snap to install
    """

    install_type: Literal["store"] = Field("store", alias="install-type", exclude=True)
    name: str = Field(exclude=True)
    classic: Optional[bool] = None
    devmode: Optional[bool] = None
    state: Optional[snap_lib.SnapState] = Field(snap_lib.SnapState.Present)
    channel: Optional[str] = None
    cohort: Optional[str] = None
    revision: Optional[str] = None

    @validator("revision", pre=True)
    def _validate_revision(cls, value: Union[str, int, None]) -> Optional[str]:
        """Validate the revision is a valid snap revision.

        Arguments:
            value: (str): The revision to validate

        Returns:
            str: The validated revision

        Raises:
            ValueError: If the revision isn't an integer
        """
        if isinstance(value, int):
            return str(value)
        if value and not re.match(r"^\d+$", value):
            raise ValueError(f"Revision is not an integer: {value}")
        return value


SnapArgument = Annotated[
    Union[SnapFileArgument, SnapStoreArgument], Field(discriminator="install_type")
]


def _local_arch() -> str:
    """Retrieve the local architecture.

    Returns:
        str: The architecture of this machine
    """
    dpkg_arch = ["dpkg", "--print-architecture"]
    return subprocess.check_output(dpkg_arch).decode("UTF-8").strip()


def _default_snap_installation() -> Path:
    """Return the default snap_installation manifest.

    Returns:
        path to the default snap_installation manifest
    """
    return Path("templates/snap_installation.yaml")


def _overridden_snap_installation() -> Path:
    """Return the overridden snap_installation manifest.

    Returns:
        path to the overridden snap_installation manifest
    """
    return Path("./snap-installation/resource/snap_installation.yaml")


def _normalize_paths(snap_installation):
    """Normalize the paths in the snap_installation manifest.

    Arguments:
        snap_installation: The path to the snap_installation manifest
    """
    snap_installation = snap_installation.resolve()
    content = yaml.safe_load(snap_installation.read_text(encoding="utf-8"))
    updated = False
    for arch, snaps in content.items():
        for idx, snap in enumerate(snaps):
            if snap.get("filename"):
                resolved = (snap_installation.parent / snap["filename"]).resolve()
                log.info("Resolving snap filename: %s to %s", snap["filename"], resolved)
                content[arch][idx]["filename"] = str(resolved)
                updated = True
    if updated:
        yaml.safe_dump(content, snap_installation.open(mode="w", encoding="utf-8"))


def _select_snap_installation(charm: ops.CharmBase) -> Path:
    """Select the snap_installation manifest.

    Arguments:
        charm: The charm instance necessary to check the unit resources

    Returns:
        path: The path to the snap_installation manifest

    Raises:
        SnapError: when the management issue cannot be resolved
    """
    try:
        resource_path = charm.model.resources.fetch("snap-installation")
    except (ops.ModelError, NameError):
        log.error("Something went wrong when claiming 'snap-installation' resource.")
        return _default_snap_installation()

    resource_size = resource_path.stat().st_size
    log.info("Resource path size: %d bytes", resource_size)
    unpack_path = _overridden_snap_installation().parent
    shutil.rmtree(unpack_path, ignore_errors=True)
    if resource_size == 0:
        log.info("Resource size is zero bytes. Use the charm defined snap installation script")
        return _default_snap_installation()

    # Unpack the snap-installation resource
    unpack_path.mkdir(parents=True, exist_ok=True)
    command = f"tar -xzvf {resource_path} -C {unpack_path} --no-same-owner"
    try:
        subprocess.check_call(shlex.split(command))
    except subprocess.CalledProcessError as e:
        log.error("Failed to extract 'snap-installation:'")
        raise snap_lib.SnapError("Invalid snap-installation resource") from e

    # Find the snap_installation manifest
    snap_installation = unpack_path / "snap_installation.yaml"
    if snap_installation.exists():
        log.info("Found snap_installation manifest")
        _normalize_paths(snap_installation)
        return snap_installation

    snap_path = list(unpack_path.glob("*.snap"))
    if len(snap_path) == 1:
        log.info("Found snap_installation snap: %s", snap_path[0])
        arch = _local_arch()
        manifest = {
            arch: [
                {
                    "install-type": "file",
                    "name": "k8s",
                    "filename": str(snap_path[0]),
                    "classic": True,
                    "dangerous": True,
                }
            ]
        }
        yaml.safe_dump(manifest, snap_installation.open("w"))
        return snap_installation

    log.error("Failed to find a snap file in snap_installation resource")
    raise snap_lib.SnapError("Failed to find snap_installation manifest")


def _parse_management_arguments(charm: ops.CharmBase) -> List[SnapArgument]:
    """Parse snap management arguments.

    Arguments:
        charm: The charm instance necessary to check the unit resources

    Raises:
        SnapError: when the management issue cannot be resolved

    Returns:
        Parsed arguments list for the specific host architecture
    """
    revision = _select_snap_installation(charm)
    if not revision.exists():
        raise snap_lib.SnapError(f"Failed to find file={revision}")
    try:
        body = yaml.safe_load(revision.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        log.error("Failed to load file=%s, %s", revision, e)
        raise snap_lib.SnapError(f"Failed to load file={revision}")

    arch = _local_arch()

    if not (isinstance(body, dict) and (arch_spec := body.get(arch))):
        log.warning("Failed to find revision for arch=%s", arch)
        raise snap_lib.SnapError(f"Failed to find revision for arch={arch}")

    try:
        args: List[SnapArgument] = [
            parse_obj_as(SnapArgument, arg) for arg in arch_spec  # type: ignore[arg-type]
        ]
    except ValidationError as e:
        log.warning("Failed to validate args=%s (%s)", arch_spec, e)
        raise snap_lib.SnapError("Failed to validate snap args")

    return args


def management(charm: ops.CharmBase) -> None:
    """Manage snap installations on this machine.

    Arguments:
        charm: The charm instance
    """
    cache = snap_lib.SnapCache()
    for args in _parse_management_arguments(charm):
        which = cache[args.name]
        if isinstance(args, SnapFileArgument) and which.revision != "x1":
            snap_lib.install_local(**args.dict(exclude_none=True))
        elif isinstance(args, SnapStoreArgument) and args.revision:
            if which.revision != args.revision:
                log.info("Ensuring %s snap revision=%s", args.name, args.revision)
                which.ensure(**args.dict(exclude_none=True))
                which.hold()
        elif isinstance(args, SnapStoreArgument):
            log.info("Ensuring %s snap channel=%s", args.name, args.channel)
            which.ensure(**args.dict(exclude_none=True))


def version(snap: str) -> Tuple[Optional[str], bool]:
    """Retrieve the version of the installed snap package.

    Arguments:
        snap: (str): Name of the snap

    Returns:
        Optional[str]: The version of the installed snap package, or None if
        not available.
    """
    overridden = _overridden_snap_installation().exists()
    try:
        result = subprocess.check_output(["/usr/bin/snap", "list", snap])
    except subprocess.CalledProcessError:
        return None, overridden

    output = result.decode().strip()
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
    if match:
        return match.group(), overridden

    log.info("Snap k8s not found or no version available.")
    return None, overridden
