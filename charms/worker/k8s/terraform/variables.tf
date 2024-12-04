# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

variable "app_name" {
  description = "Name of the application in the Juju model."
  type        = string
  default     = "k8s"
}

variable "channel" {
  description = "The channel to use when deploying a charm."
  type        = string
  default     = "1.30/edge"
}

variable "config" {
  description = "Application config. Details about available options can be found at https://charmhub.io/k8s/configurations."
  type        = map(string)
  default     = {}
}

variable "constraints" {
  description = "Juju constraints to apply for this application."
  type        = string
  default     = "arch=amd64"
}

variable "model" {
  description = "Reference to a `juju_model`."
  type        = string
  default     = ""
}

variable "resources" {
  description = "Resources to use with the application. Details about available options can be found at https://charmhub.io/k8s/configurations."
  type        = map(string)
  default     = {}
}

variable "revision" {
  description = "Revision number of the charm"
  type        = number
  default     = null
}

variable "base" {
  description = "Ubuntu base to deploy the charm onto"
  type        = string
  default     = "24.04"

  validation {
    condition     = contains(["20.04", "22.04", "24.04"], var.base)
    error_message = "Base must be one of 20.04, 22.04, 24.04"
  }
}

variable "units" {
  description = "Number of units to deploy"
  type        = number
  default     = 1
}
