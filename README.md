<!--
Avoid using this README file for information that is maintained or published elsewhere, e.g.:

* metadata.yaml > published on Charmhub
* documentation > published on (or linked to from) Charmhub
* detailed contribution guide > documentation or CONTRIBUTING.md

Use links instead.
-->

# k8s charms

[![Unit Tests](https://github.com/canonical/k8s-operator/actions/workflows/test.yaml/badge.svg)](https://github.com/canonical/k8s-operator/actions/workflows/test.yaml)
[![Integration Tests](https://github.com/canonical/k8s-operator/actions/workflows/integration_test.yaml/badge.svg)](https://github.com/canonical/k8s-operator/actions/workflows/integration_test.yaml)

Charmhub package name: k8s
More information: https://charmhub.io/k8s

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
