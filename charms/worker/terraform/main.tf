# Copyright 2026 Canonical Ltd.
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

  config            = var.config
  constraints       = var.constraints
  endpoint_bindings = var.endpoint_bindings
  machines          = var.machines
  resources         = var.resources
  units             = var.units
}
