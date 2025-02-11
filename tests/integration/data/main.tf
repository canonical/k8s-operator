# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

terraform {
  required_version = ">= 1.6"
  required_providers {
    juju = {
      source  = "juju/juju"
      version = "~> 0.16.0"
    }
  }
}

provider "juju" {}

variable "manifest_yaml" {
  description = "Path to the manifest YAML file"
  type        = string
  default     = "default-manifest.yaml"
}

variable "model" {
  description = "Name of the model to deploy to"
  type        = string
  default     = "my-canonical-k8s"
}

module "k8s" {
  source        = "git::https://github.com/canonical/k8s-bundles//terraform?ref=KU-2592/terraform-ceph"
  model         = var.model
  manifest_yaml = var.manifest_yaml
}
