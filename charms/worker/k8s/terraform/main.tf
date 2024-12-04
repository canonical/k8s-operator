# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "k8s" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "k8s"
    channel  = var.channel
    revision = var.revision
    bases   = var.base
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  resources   = var.resources
}
