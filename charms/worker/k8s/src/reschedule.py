# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module used to create scheduled hook events by abusing juju secret expirations."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import ops

log = logging.getLogger(__name__)
PERIODIC_LABEL = "{unit}-{name}-periodic"
Period = timedelta  # Type aliasing


def _model(event: ops.EventBase) -> ops.Model:
    return event.framework.model


def _secret_find(event: ops.EventBase, **selector) -> Optional[ops.Secret]:
    model = _model(event)
    secret = None
    try:
        secret = model.get_secret(**selector)
    except ops.SecretNotFoundError:
        log.info("No Secret found matching %s", selector)
    else:
        log.info("Secret found matching %s: id=%s", selector, secret.id)
    return secret


def _secret_delete(event: ops.EventBase, **selector):
    if secret := _secret_find(event, **selector):
        info = secret.get_info()
        if info.id:
            log.info("Removing secret matching %s: id=%d rev=%s", selector, info.id, info.revision)
            secret.remove_revision(revision=info.revision)


def _secret_create(event: ops.EventBase, period: Period, **selector) -> ops.Secret:
    unit = _model(event).unit
    log.info("Creating Secret matching %s with period %s", selector, period)
    unit.add_secret(
        content={"secret": "abuse"},
        description="Secret used to reschedule hook events on this unit",
        expire=period,
        **selector
    )


@dataclass
class PeriodicEvent:
    name: str

    def _label(self, event: ops.EventBase):
        model = _model(event)
        return PERIODIC_LABEL.format(unit=model.unit.name, name=self.name)

    def cancel(self, event: ops.EventBase):
        log.info("Cancelling timer for %s", self.name)
        _secret_delete(event, label=self._label(event))

    def create(self, event: ops.EventBase, period: Period):
        log.info("Creating timer for %s", self.name)
        _secret_create(event, period, label=self._label(event))
