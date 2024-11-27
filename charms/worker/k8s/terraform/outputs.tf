# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.k8s.name
}

output "requires" {
  value = {
    aws         = "aws-integration"
    azure       = "azure-integration"
    cluster     = "k8s-cluster"
    cos_tokens  = "cos-k8s-tokens"
    containerd  = "containerd"
    gcp         = "gcp-integration"
  }
}

output "provides" {
  value = {
    cos_agent   = "cos_agent"
  }
}
