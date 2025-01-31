# K8s Charm

This defines a charm named `k8s`, an application which deploys the `k8s` snap providing an opinioned distribution of Canonical Kubernetes within juju which is extensible, manage-able, and observable via the juju eco-system. The units deployed by this charm operate multiple kubernetes binaries maintained by Canonical (`kubelet`, `kube-proxy`, `kube-scheduler`, `kube-controller-manager`, and `kube-apiserver`)

## Deploy

Deploy the charm from the charmhub store with a single command to have a single node cluster.

```sh
juju deploy k8s
```

## Adding Units

Adding more units to the kubernetes cluster is accomplished by adding more units.

```sh
juju add-unit k8s
```

## Configuring

The charm's `charmcraft.yaml` defines  options for every node in the kubernetes cluster exp
