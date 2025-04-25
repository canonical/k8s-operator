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

  config            = var.config
  constraints       = var.constraints
  endpoint_bindings = var.endpoint_bindings
  placement         = var.placement
  resources         = var.resources
  units             = var.units
}
