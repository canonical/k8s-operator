# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "k8s_worker" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "k8s-worker"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  resources   = var.resources

  # Juju converts GB into MB internally for constraints.
  # This let's terraform fail as expected state != actual state.
  # This is a workaround to ignore the constraints change.
  lifecycle {
    ignore_changes = [constraints]
  }
}
