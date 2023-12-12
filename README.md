# k8s charms

[![Tests K8s](https://github.com/canonical/k8s-operator/actions/workflows/tests-k8s.yaml/badge.svg)](https://github.com/canonical/k8s-operator/actions/workflows/tests-k8s.yaml)
[![Tests K8s Worker](https://github.com/canonical/k8s-operator/actions/workflows/tests-k8s-worker.yaml/badge.svg)](https://github.com/canonical/k8s-operator/actions/workflows/tests-k8s-worker.yaml)
[![Integration Tests](https://github.com/canonical/k8s-operator/actions/workflows/integration_test.yaml/badge.svg)](https://github.com/canonical/k8s-operator/actions/workflows/integration_test.yaml)
<!-- Remove this comment when the charms are published on CharmHub

 [![Get K8s from the Charmhub](https://charmhub.io/k8s/badge.svg)](https://charmhub.io/k8s)
 [![Get K8s-Worker from the Charmhub](https://charmhub.io/k8s-worker/badge.svg)](https://charmhub.io/k8s-worker)

  Charmhub package name: k8s
  More information: https://charmhub.io/k8s

  Charmhub package name: k8s-worker
  More information: https://charmhub.io/k8s-worker
-->

A machine charm which operates a complete Kubernetes cluster.

This charm installs and operates a Kubernetes cluster via the k8s snap. It exposes
relations to co-operate with other kubernetes components such as optional CNIs, 
optional cloud-providers, optional schedulers, external backing stores, and external
certificate managers.

This charm provides the following running components:
* kube-apiserver
* kube-scheduler
* kube-controller-manager
* kube-proxy
* kubelet
* containerd

This charm can optionally disable the following components:
* A Kubernetes Backing Store
* A Kubernetes CNI

## Other resources

- [Contributing](CONTRIBUTING.md) <!-- or link to other contribution documentation -->

- See the [Juju SDK documentation](https://juju.is/docs/sdk) for more information about developing and improving charms.
