# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Literal definitions shared by the integration tests."""

from pathlib import Path

# Durations
ONE_MIN = "1m"

# Waiting
# jubilant's Juju.wait() polls `juju status`; DEFAULT_DELAY seconds between polls and
# DEFAULT_SUCCESSES consecutive successes approximate libjuju's idle_period of 30s.
DEFAULT_DELAY = 5.0
DEFAULT_SUCCESSES = 6

# Paths
REPO_ROOT = Path(__file__).parent.parent.parent
TEST_DATA = Path(__file__).parent / "data"
DEFAULT_SNAP_INSTALLATION = TEST_DATA / "default-snap-installation.tar.gz"
STATIC_PROXY_CONFIG = TEST_DATA / "static-proxy-config.yaml"

# Charms built from this repository, mapped to their charmcraft project directory.
CHARMCRAFT_DIRS = {
    "k8s": REPO_ROOT / "charms/worker/k8s",
    "k8s-worker": REPO_ROOT / "charms/worker",
}

# Ubuntu base the test bundles deploy onto unless --base or a marking overrides it.
DEFAULT_BASE = "ubuntu@24.04"
