# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Introspection of, and setup for, the cloud the tests are running against."""

import functools
import logging
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
        juju.model_config({"container-networking-method": "local", "fan-config": ""})


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
