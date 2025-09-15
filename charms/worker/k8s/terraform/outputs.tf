# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.k8s.name
}

output "requires" {
  value = {
    aws                     = "aws"
    azure                   = "azure"
    etcd                    = "etcd"
    external_cloud_provider = "external-cloud-provider"
    gcp                     = "gcp"
    external_load_balancer  = "external-load-balancer"
  }
}

output "provides" {
  value = {
    cos_agent         = "cos-agent"
    cos_worker_tokens = "cos-worker-tokens"
    containerd        = "containerd"
    ceph_k8s_info     = "ceph-k8s-info"
    k8s_cluster       = "k8s-cluster"
    kube_control      = "kube-control"
  }
}
