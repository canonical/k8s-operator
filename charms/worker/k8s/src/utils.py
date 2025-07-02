# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utility functions."""

import logging
import re

import ops.log

# Constants for time calculations
YEAR_SECONDS = 365 * 24 * 60 * 60  # seconds in a year
MONTH_SECONDS = 30 * 24 * 60 * 60  # seconds in a month
DAY_SECONDS = 24 * 60 * 60  # seconds in a day


def ttl_to_seconds(ttl: str):
    """Convert a TTL string to seconds.

    Args:
        ttl: A string representing time to live (e.g., "1y", "6mo", "7d", "24h")

    Returns:
        The number of seconds represented by the TTL

    Raises:
        ValueError: If the TTL format is invalid
    """
    pattern = r"^(\d+)(y|mo|d|h|m|s)$"
    match = re.fullmatch(pattern, ttl, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid TTL format: {ttl}")

    value, unit = match.groups()
    value = int(value)
    unit = unit.lower()

    multipliers = {
        "y": YEAR_SECONDS,
        "mo": MONTH_SECONDS,
        "d": DAY_SECONDS,
        "h": 3600,
        "m": 60,
        "s": 1,
    }

    if unit not in multipliers:
        raise ValueError(f"Invalid TTL unit: {unit}")

    return value * multipliers[unit]


def setup_root_logger():
    """Improve the JujuLogHandler to include logger name and lineno."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, ops.log.JujuLogHandler):
            handler.setFormatter(logging.Formatter("%(name)s:%(lineno)d %(message)s"))
