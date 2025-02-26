# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_version = ">= 1.6"
  required_providers {
    juju = {
      source  = "juju/juju"
      version = "~> 0.14.0"
    }
  }
}

provider "juju" {}

variable "manifest_yaml" {
  description = "Path to the manifest YAML file"
  type        = string
  default     = "k8s-manifest.yaml"
}

variable "model" {
  description = "Name of the model to deploy to"
  type        = string
  default     = "my-canonical-k8s"
}

module "k8s" {
  source        = "git::https://github.com/canonical/k8s-bundles//terraform?ref=main"
  model         = var.model
  manifest_yaml = var.manifest_yaml
}
