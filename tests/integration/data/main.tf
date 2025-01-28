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

variable "manifest_path" {
  description = "Path to the manifest YAML file"
  type        = string
  default     = "default-manifest.yaml"
}

variable "model_name" {
  description = "Name of the model to deploy to"
  type        = string
  default     = "my-canonical-k8s"
}

module "k8s" {
  source        = "git::https://github.com/asbalderson/k8s-bundles//terraform?ref=terraform-bundle-basic"
  model         = var.model_name
  manifest_yaml = var.manifest_path
}
