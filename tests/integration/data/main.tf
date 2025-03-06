# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_version = ">= 1.6"
  required_providers {
    juju = {
      source  = "juju/juju"
      version = ">= 0.14.0, < 1.0.0"
    }
  }
}

variable "manifest_yaml" {
  description = "Path to the manifest YAML file"
  type        = string
}

variable "model" {
  description = "Name of the model to deploy to"
  type        = string
}

variable "cloud" {
  description = "Cloud to deploy to"
  type        = string
}

variable "cloud_integration" {
  description = "Selection of a cloud integration."
  type        = string
  default     = ""
}

variable "csi_integration" {
  description = "Selection of a csi integration."
  type        = list(string)
  default     = []
}

module "k8s" {
  source        = "git::https://github.com/canonical/k8s-bundles//terraform?ref=main"
  model         = {
    name = var.model
    cloud = var.cloud
    config = {
      "test" = true
    }
  }
  cloud_integration = var.cloud_integration
  csi_integration = var.csi_integration
  manifest_yaml = var.manifest_yaml
}
