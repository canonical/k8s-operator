# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "k8s_worker" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "k8s-worker"
    channel  = var.channel
    revision = var.revision
    series   = var.series
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  resources   = var.resources
}
