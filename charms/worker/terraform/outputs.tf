# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.k8s_worker.name
}

output "requires" {
  value = {
    aws        = "aws"
    azure      = "azure"
    cluster    = "cluster"
    cos_tokens = "cos-tokens"
    containerd = "containerd"
    gcp        = "gcp"
  }
}

output "provides" {
  value = {
    cos_agent = "cos-agent"
  }
}

output "machines" {
  value = juju_application.k8s.machines
}
