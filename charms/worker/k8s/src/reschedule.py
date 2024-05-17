# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""EventTimer for scheduling dispatch of juju event on regular intervals."""

import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Tuple, TypedDict

import ops

log = logging.getLogger(__name__)
Period = timedelta  # Type aliasing
BIN_SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_SERVICE = """
[Unit]
Description=Dispatch the {event} event on {unit}

[Service]
Type=oneshot
ExecStart=/usr/bin/timeout {timeout} /usr/bin/bash -c '/usr/bin/juju-exec "{unit}" "JUJU_DISPATCH_PATH={event} ./dispatch"'

[Install]
WantedBy=multi-user.target
"""
SYSTEMD_TIMER = """
[Unit]
Description=Timer to dispatch {event} event periodically
Requires={app}.{event}.service

[Timer]
Unit={app}.{event}.service
OnUnitInactiveSec={interval}m
RandomizedDelaySec={random_delay}m

[Install]
WantedBy=timers.target
"""


def execute_command(args: List[str], check_exit: bool = True) -> Tuple[str, int]:
    """Subprocess run wrapper.

    Args:
        args (List[str]): arguments to subprocess
        check_exit (bool): True if we raise on errors

    Returns:
        stdout, int : tuple to handled output.
    """
    s = subprocess.run(args, capture_output=True, check=check_exit)
    return s.stdout.decode(), s.returncode


class TimerError(Exception):
    """Generic timer error as base exception."""


class TimerEnableError(TimerError):
    """Raised when unable to enable a event timer."""


class TimerDisableError(TimerError):
    """Raised when unable to disable a event timer."""


class TimerStatusError(TimerError):
    """Raised when unable to check status of a event timer."""


class EventConfig(TypedDict):
    """Configuration used by service and timer templates.

    Attributes:
        app: Name of the juju application.
        event: Name of the event.
        interval: Minutes between the event trigger.
        random_delay: Minutes of random delay added between event trigger.
        timeout: Minutes before the event handle is timeout.
        unit: Name of the juju unit.
    """

    app: str
    event: str
    interval: int
    random_delay: int
    timeout: int
    unit: str


class EventTimer:
    """Manages the timer to emit juju events at regular intervals.

    Attributes:
        unit_name (str): Name of the juju unit to emit events to.
    """

    _systemd_path = Path("/etc/systemd/system")

    def __init__(self, unit: ops.Unit):
        """Construct the timer manager.

        Args:
            unit: Name of the juju unit to emit events to.
        """
        self._unit = unit
        self.unit_name = unit.name
        self.app_name = unit.app.name

    def _render_event_template(
        self, template_type: str, event_name: str, context: EventConfig
    ) -> None:
        """Write event configuration files to systemd path.

        Args:
            template_type: Name of the template type to use. Can be 'service' or 'timer'.
            event_name: Name of the event to schedule.
            context: Addition configuration for the event to schedule.
        """
        template = SYSTEMD_SERVICE if "service" == template_type else SYSTEMD_TIMER
        dest = self._systemd_path / f"{self.app_name}.{event_name}.{template_type}"
        dest.write_text(template.format(**context))

    def is_active(self, event_name: str) -> bool:
        """Check if the systemd timer is active for the given event.

        Args:
            event_name: Name of the juju event to check.

        Returns:
            True if the timer is enabled, False otherwise.

        Raises:
            TimerStatusError: Timer status cannot be determined.
        """
        try:
            # We choose status over is-active here to provide debug logs that show the output of
            # the timer.
            _, ret_code = execute_command(
                [BIN_SYSTEMCTL, "status", f"{self.app_name}.{event_name}.timer"], check_exit=False
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
            raise TimerStatusError from ex

        return ret_code == 0

    def ensure_event_timer(
        self, event_name: str, interval: int, timeout: Optional[int] = None
    ) -> None:
        """Ensure that a systemd service and timer are registered to dispatch the given event.

        The interval is how frequently, in minutes, the event should be dispatched.

        The timeout is the number of seconds before an event is timed out. If not set or 0,
        it defaults to half the interval period.

        Args:
            event_name: Name of the juju event to schedule.
            interval: Number of minutes between emitting each event.
            timeout: Timeout for each event handle in minutes.

        Raises:
            TimerEnableError: Timer cannot be started. Events will be not emitted.
        """
        if timeout is not None:
            timeout_in_secs = timeout * 60
        else:
            timeout_in_secs = interval * 30

        context: EventConfig = {
            "event": event_name,
            "interval": interval,
            "random_delay": interval // 4,
            "timeout": timeout_in_secs,
            "unit": self.unit_name,
            "app": self.app_name,
        }
        self._render_event_template("service", event_name, context)
        self._render_event_template("timer", event_name, context)

        systemd_timer = f"{self.app_name}.{event_name}.timer"
        try:
            execute_command([BIN_SYSTEMCTL, "daemon-reload"])
            execute_command([BIN_SYSTEMCTL, "enable", systemd_timer])
            execute_command([BIN_SYSTEMCTL, "start", systemd_timer])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
            raise TimerEnableError(f"Unable to enable systemd timer {systemd_timer}") from ex

    def disable_event_timer(self, event_name: str) -> None:
        """Disable the systemd timer for the given event.

        Args:
            event_name: Name of the juju event to disable.

        Raises:
            TimerDisableError: Timer cannot be stopped. Events will be emitted continuously.
        """
        systemd_timer = f"{self.app_name}.{event_name}.timer"
        try:
            # Don't check for errors in case the timer wasn't registered.
            execute_command([BIN_SYSTEMCTL, "stop", systemd_timer], check_exit=False)
            execute_command([BIN_SYSTEMCTL, "disable", systemd_timer], check_exit=False)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
            raise TimerDisableError(f"Unable to disable systemd timer {systemd_timer}") from ex


class PeriodicEvent:
    """Manages the event trigger."""

    def __init__(self, charm: ops.CharmBase, event_name: str = "update_status"):
        """Crafts a PeriodicEvent.

        Args:
            charm (ops.CharmBase): charm object to retrigger.
            event_name (str):      name of the juju event to retrigger.
        """
        self._name = event_name
        self._timer = EventTimer(charm.unit)

    def create(self, period: Period):
        """Ensure that a periodic timer is active.

        Args:
            period (Period): timedelta determining how long to wait before trigger
        """
        log.info("Creating timer for %s", self._name)
        if not self._timer.is_active(self._name):
            self._timer.ensure_event_timer(self._name, period.seconds // 60)

    def cancel(self):
        """Cancel any active triggers."""
        log.info("Cancelling timer for %s", self._name)
        if self._timer.is_active(self._name):
            self._timer.disable_event_timer(self._name)
