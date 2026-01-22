terraform {
  # Copyright 2026 Canonical Ltd.
  # See LICENSE file for licensing details.

  required_version = ">= 1.6"
  required_providers {
    juju = {
      source  = "juju/juju"
      version = ">= 0.14.0, < 1.0.0"
    }
  }
}
