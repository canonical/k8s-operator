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
    etcd        = "etcd"
    external_cloud_provider = "external_cloud_provider"
    gcp         = "gcp-integration"
  }
}

output "provides" {
  value = {
    cos_agent         = "cos_agent"
    cos_worker_tokens = "cos-k8s-tokens"
    containerd        = "containerd"
    ceph_k8s_info     = "kubernetes-info"
    k8s_cluster       = "k8s-cluster"
    kube_contro       = "kube-control"
  }
}
