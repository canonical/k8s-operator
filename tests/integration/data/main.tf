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

variable "manifest_path" {
  description = "Path to the manifest YAML file"
  type        = string
  default     = "./default-manifest.yaml"
}

variable "model_name" {
  description = "Name of the model to deploy to"
  type        = string
  default     = "my-canonical-k8s"
}

module "k8s" {
  source        = "git::https://github.com/asbalderson/k8s-bundles//terraform?ref=terraform-bundle-basic"
  model         = var.model_name
  # TODO: This should be set to the path of the manifest file
  manifest_yaml = var.manifest_path
}
