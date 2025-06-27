# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests reschedule module."""

import subprocess
from unittest import mock

import pytest

import reschedule


@pytest.fixture
def harness(harness):
    """Craft a ops test harness only for the control-plane."""
    if harness.charm.is_worker:
        pytest.skip("Not applicable on workers")
    yield harness


@mock.patch("reschedule.subprocess.run")
def test_execute_command(subprocess_run):
    """Test no file exists."""
    subprocess_run.return_value.returncode = 0

    args = ["cat", "/made/up/file"]
    rc = reschedule._execute_command(args)

    subprocess_run.assert_called_once_with(args, check=True)
    assert rc == 0


def test_event_timer_properties(harness):
    """Test Event Timer properties."""
    et = reschedule.EventTimer(harness.charm.unit)
    assert et.unit_num == 0
    assert et.app_name == "k8s"


@mock.patch("reschedule._execute_command")
def test_event_timer_is_active(_exec, harness):
    """Test Event Timer is_active."""
    _exec.return_value = 0

    et = reschedule.EventTimer(harness.charm.unit)
    assert et.is_active("update-status")

    _exec.return_value = -1
    et = reschedule.EventTimer(harness.charm.unit)
    assert not et.is_active("update-status")

    _exec.side_effect = subprocess.CalledProcessError(-1, [])
    et = reschedule.EventTimer(harness.charm.unit)
    with pytest.raises(reschedule.TimerStatusError):
        assert not et.is_active("update-status")


@mock.patch("reschedule.Path.write_text")
def test_render_event_template(write_text, harness):
    """Test renders event template."""
    context = {
        "app": "k8s",
        "event": "update-status",
        "interval": 30,
        "random_delay": 7,
        "timeout": 15,
        "unit_num": 0,
    }
    et = reschedule.EventTimer(harness.charm.unit)
    et._render_event_template("service", "update-status", context)
    write_text.assert_called_once()


@mock.patch("reschedule._execute_command")
def test_event_timer_ensure(_exec, harness):
    """Test ensure on event timer."""
    _exec.return_value = ("", 0)

    et = reschedule.EventTimer(harness.charm.unit)
    with mock.patch.object(et, "_render_event_template") as rendered:
        et.ensure("update-status", 30)

    context = {
        "app": "k8s",
        "event": "update-status",
        "interval": 30,
        "random_delay": 7,
        "timeout": 15,
        "unit_num": 0,
    }
    rendered.assert_has_calls(
        [
            mock.call("service", "update-status", context),
            mock.call("timer", "update-status", context),
        ]
    )


@mock.patch("reschedule._execute_command")
def test_event_timer_disable(_exec, harness):
    """Test disable on event timer."""
    _exec.return_value = ("", 0)

    et = reschedule.EventTimer(harness.charm.unit)
    et.disable("update-status")
    sysctl = reschedule.BIN_SYSTEMCTL
    calls = [
        mock.call([sysctl, "stop", "k8s.update-status.timer"], check_exit=False),
        mock.call([sysctl, "disable", "k8s.update-status.timer"], check_exit=False),
    ]
    _exec.assert_has_calls(calls)


@mock.patch("reschedule.EventTimer.ensure")
@mock.patch("reschedule.EventTimer.is_active")
def test_periodic_event_create(is_active, ensure, harness):
    """Test creating a periodic event."""
    pe = reschedule.PeriodicEvent(harness.charm)
    is_active.return_value = False
    pe.create(reschedule.Period(minutes=10))
    ensure.assert_called_once_with("update_status", 600)


@mock.patch("reschedule.EventTimer.disable")
@mock.patch("reschedule.EventTimer.is_active")
def test_periodic_event_cancel(is_active, disable, harness):
    """Test cancelling a periodic event."""
    pe = reschedule.PeriodicEvent(harness.charm)
    is_active.return_value = True
    pe.cancel()
    disable.assert_called_once_with("update_status")
