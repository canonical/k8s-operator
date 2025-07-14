# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk
"""Node client module.

this module defines the behaviors around determining the status of a node.
"""

import enum
import subprocess
from pathlib import Path

from k8s.client import kubectl


class Presence(enum.Enum):
    """Presence of the node in the kubernetes cluster."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNRESPONSIVE = "unresponsive"


class Status(enum.Enum):
    """Status of the node in the kubernetes cluster."""

    READY = "ready"
    NOT_READY = "not ready"
    UNRESPONSIVE = "unresponsive"


def present(kubeconfig: Path, node: str) -> Presence:
    """Determine if node is in the kubernetes cluster.

    Args:
        kubeconfig (Path): path to kubeconfig
        node (str): name of node

    Returns:
        Presence: Available, Unavailable, or Unknown
    """
    cmd = ["get", "nodes", node, "-o=jsonpath={.metadata.name}"]
    try:
        name = kubectl(*cmd, kubeconfig=kubeconfig)
    except subprocess.CalledProcessError as e:
        if e.returncode == 1 and "not found" in e.output:
            return Presence.UNAVAILABLE
        return Presence.UNRESPONSIVE
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return Presence.UNRESPONSIVE
    return Presence.UNAVAILABLE if name != node else Presence.AVAILABLE


def ready(kubeconfig: Path, node: str) -> Status:
    """Determine if node is Ready in the kubernetes cluster.

    Args:
        kubeconfig (Path): path to kubeconfig
        node (str): name of node

    Returns:
        Status: Ready, NotReady, or Unknown
    """
    cmd = ["get", "nodes", node, '-o=jsonpath={.status.conditions[?(@.type=="Ready")].status}']
    try:
        readiness = kubectl(*cmd, kubeconfig=kubeconfig)
    except subprocess.CalledProcessError as e:
        if e.returncode == 1 and "not found" in e.output:
            # Not finding a node is a not ready node
            return Status.NOT_READY
        return Status.UNRESPONSIVE
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return Status.UNRESPONSIVE
    return Status.NOT_READY if readiness != "True" else Status.READY
