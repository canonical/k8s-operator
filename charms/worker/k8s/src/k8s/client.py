# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""K8s client module.

This module acts as a clear client towards this clusters
Kubernetes API.  Currently its implemented through kubectl
against the cluster, but in future could be better replaced
with API command.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

from literals import KUBECTL_PATH

log = logging.getLogger(__name__)


def kubectl(*args: str, kubeconfig: Optional[Path] = None, **kwds) -> str:
    """Run kubectl command.

    Arguments:
        args: arguments passed to kubectl
        kubeconfig: keyword-only argument specifying which config file to use
        kwds: keyword arguments passed to subprocess.check_output

    Returns:
        string response

    Raises:
        CalledProcessError: in the event of a failed kubectl
        TimeoutExpired: in the event of a timeout
    """
    cmd = [KUBECTL_PATH.as_posix()]
    if kubeconfig:
        cmd += [f"--kubeconfig={kubeconfig}"]
    cmd += args
    subprocess_kwds = {"text": True, "timeout": 30, **kwds}
    log.info("%s, %s", " ".join(cmd), subprocess_kwds)
    try:
        return subprocess.check_output(cmd, **subprocess_kwds)
    except subprocess.CalledProcessError as e:
        log.error("Command failed: %s\nreturncode: %s\nstdout: %s", cmd, e.returncode, e.output)
        raise
    except subprocess.TimeoutExpired as e:
        log.error("Command timeout: %s\nstdout: %s", cmd, e.output)
        raise
