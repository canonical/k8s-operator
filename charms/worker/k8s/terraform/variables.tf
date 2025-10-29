# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

variable "app_name" {
  description = "Name of the application in the Juju model."
  type        = string
  default     = "k8s"
}

variable "base" {
  description = "Ubuntu base to deploy the charm onto"
  type        = string
  default     = "ubuntu@24.04"

  validation {
    condition     = contains(["ubuntu@22.04", "ubuntu@24.04"], var.base)
    error_message = "Base must be one of ubuntu@22.04, ubuntu@24.04"
  }
}

variable "channel" {
  description = "The channel to use when deploying a charm."
  type        = string
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

variable "endpoint_bindings" {
  description = "Endpoint bindings for the application."
  type        = map(string)
  default     = {}
}

variable "model" {
  description = "Reference to a `juju_model`."
  type        = string
}

variable "placement" {
  description = "Placement constraints for the application."
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

variable "units" {
  description = "Number of units to deploy"
  type        = number
  default     = 1
}

variable "expose" {
  description = "How to expose the Kubernetes API endpoint"
  type        = map(string)
  default     = null

  validation {
    condition = (
      # If expose is null, it's valid
      var.expose == null ||
      # If expose is a map, it can only contain specific keys
      length(setsubtract(keys(var.expose), ["cidrs", "endpoints", "spaces"])) == 0
    )
    error_message = "If provided, expose must only contain the keys: cidrs, endpoints, spaces."
  }
}
