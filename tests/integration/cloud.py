# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Introspection of, and setup for, the cloud the tests are running against."""

import functools
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import jubilant
import pytest
import yaml
from helpers import get_unit_cidrs
from literals import STATIC_PROXY_CONFIG
from lxd_substrate import LXDSubstrate

log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=None)
def cloud_type(model: Optional[str], lxd_containers: bool) -> Tuple[str, bool]:
    """Return the provider type of the model's cloud, and whether VMs are used.

    ``juju show-model`` reports the cloud *provider* type (``lxd``, ``openstack``, ``ec2``)
    in its ``type`` field, which jubilant exposes as ``ModelInfo.type``. Note that
    ``Status.model.type`` is something else entirely (``iaas``/``caas``).

    Args:
        model: Model name; None means the currently selected model. Also the cache key.
        lxd_containers: Value of the ``--lxd-containers`` flag.

    Returns:
        Tuple of provider type and whether machines are VMs.
    """
    provider = jubilant.Juju(model=model).show_model().type
    vms = True  # Assume VMs are enabled.
    if provider == "lxd":
        vms = not lxd_containers
    return provider, vms


@functools.lru_cache(maxsize=None)
def cloud_arch(controller: Optional[str]) -> str:
    """Return the architecture of the controller's machines.

    Args:
        controller: Controller name, or None for the current controller.

    Returns:
        Architecture string, for example ``amd64``.
    """
    model = f"{controller}:controller" if controller else "controller"
    arches = set()
    for machine in jubilant.Juju(model=model).status().machines.values():
        for pair in (machine.hardware or "").split():
            key, _, value = pair.partition("=")
            if key == "arch" and value:
                arches.add(value)
    assert len(arches) == 1, f"Could not determine a single controller arch: {arches}"
    return arches.pop()


@functools.lru_cache(maxsize=None)
def juju_major(model: Optional[str]) -> int:
    """Return the major version of the Juju agents running the model.

    Args:
        model: Model name; None means the currently selected model. Also the cache key.

    Returns:
        Major version, for example ``3`` or ``4``.
    """
    version = jubilant.Juju(model=model).status().model.version
    return int(version.split(".", 1)[0])


def authorize_ssh_keys(juju: jubilant.Juju) -> None:
    """Add the client's public SSH keys to the model on Juju 4.

    Juju 3 adds them to every new model; Juju 4 no longer does, which leaves
    juju-crashdump unable to collect machine logs over SSH. The Juju 4 client
    also no longer creates its own key, which juju-crashdump always loads, so
    generate it when missing.

    Args:
        juju: Jubilant Juju instance.
    """
    if juju_major(juju.model) < 4:
        return
    juju_data = Path(os.environ.get("JUJU_DATA") or Path.home() / ".local/share/juju")
    juju_key = juju_data / "ssh/juju_id_rsa"
    if not juju_key.is_file():
        juju_key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "rsa", "-N", "", "-f", str(juju_key)], check=True
        )
    paths = [juju_key.with_suffix(".pub"), *sorted((Path.home() / ".ssh").glob("id_*.pub"))]
    # Juju 4.0.15+ rejects an argument that holds more than one key.
    keys = [
        line.strip()
        for path in paths
        if path.is_file()
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    if not keys:
        log.warning("No public SSH keys found to add to model %s", juju.model)
        return
    try:
        juju.cli("add-ssh-key", *keys)
    except jubilant.CLIError as exc:
        # Only crash dumps depend on these keys, so don't fail the deployment.
        log.warning("Could not add SSH keys to model %s: %s", juju.model, exc.stderr)


def model_owner(juju: jubilant.Juju) -> str:
    """Return the owner of the model.

    ``ModelInfo.name`` is the fully-qualified ``<owner>/<model>`` form.

    Args:
        juju: Jubilant Juju instance.

    Returns:
        The model owner's username.
    """
    name = juju.show_model().name
    owner, _, _ = name.rpartition("/")
    return owner or "admin"


def cloud_profile(
    juju: jubilant.Juju,
    provider: str,
    cloud_marker: Optional[pytest.Mark],
) -> None:
    """Apply cloud-specific settings to the model before deploying into it.

    On LXD this creates the networks and the machine profile named after the model, so that
    machines picked up by juju inherit them. On EC2/OpenStack it adjusts container
    networking.

    Args:
        juju: Jubilant Juju instance.
        provider: Cloud provider type, from :func:`cloud_type`.
        cloud_marker: The test module's ``clouds`` marker, if any.
    """
    if provider == "lxd":
        lxd = LXDSubstrate()
        lxd_profiles, lxd_networks = [], []
        if cloud_marker and "lxd" in cloud_marker.args:
            lxd_networks.extend(cloud_marker.kwargs.get("networks") or [])
            lxd_profiles.extend(cloud_marker.kwargs.get("profiles") or [])

        info = juju.show_model()
        profile_name = f"juju-{info.short_name}-{info.model_uuid[:6]}"
        lxd.configure_networks(lxd_networks)
        lxd.remove_profile(profile_name)
        lxd.apply_profile(lxd_profiles, profile_name)
    elif provider in ("ec2", "openstack"):
        model_config = {"container-networking-method": "local"}
        if juju_major(juju.model) < 4:
            # Fan networking, and its `fan-config` key, were removed in Juju 4.
            model_config["fan-config"] = ""
        juju.model_config(model_config)


def cloud_proxied(juju: jubilant.Juju) -> None:
    """Apply the expected proxy configuration to the model.

    Args:
        juju: Jubilant Juju instance.
    """
    proxy_configs = yaml.safe_load(STATIC_PROXY_CONFIG.read_text())
    controller_juju = jubilant.Juju(model=f"{juju.show_model().controller_name}:controller")
    local_no_proxy = get_unit_cidrs(controller_juju, "controller", 0)
    no_proxy = {*proxy_configs["juju-no-proxy"], *local_no_proxy}
    proxy_configs["juju-no-proxy"] = ",".join(sorted(no_proxy))
    juju.model_config(proxy_configs)
