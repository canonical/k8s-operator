# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

resource "juju_application" "k8s" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "k8s"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  expose {
    # if var.expose doesn't have a cidrs key, default to "0.0.0.0/0"
    # if var.expose.cidrs = null, don't expose the application
    cidrs = contains(try(keys(var.expose), []), "cidrs") ? var.expose.cidrs : "0.0.0.0/0"
    # if var.expose.endpoints exists, expose via endpoints
    endpoints = try(var.expose.endpoints, null)
    # if var.expose.spaces exists, expose via spaces
    spaces    = try(var.expose.spaces, null)
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  resources   = var.resources
}
