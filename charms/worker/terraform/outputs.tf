# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.k8s_worker.name
}

output "requires" {
  value = {
    aws         = "aws"
    azure       = "azure"
    cluster     = "k8s"
    cos_tokens  = "cos-tokens"
    containerd  = "containerd"
    gcp         = "gcp"
  }
}

output "provides" {
  value = {
    cos_agent   = "cos-agent"
  }
}
