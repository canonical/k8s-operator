# Contributing

## Structure of the charms

The `k8s` and `k8s-worker` charms are noticeably tucked into one-another.

```
└── worker
    ├── charmcraft.yaml
    └── k8s
        ├── charmcraft.yaml
        ├── lib
        │   └── charms/...
        ├── requirements.txt
        └── src
            └── charm.py
```

While unfamiliar to some charm developers, this lets both charms share the exact same `src` folder. This is accomplished by using the `parts.charm.charm-entrypoint` value in the `worker` directory set to `k8s/src/charm.py`.

### What's unique

The unique parts of the charm are what are in each charm's top-level directory:

```
charmcraft.yaml
.jujuignore
icon.svg
README.md
```

In order to exclude the `k8s` exclusive components from the `k8s-worker` charm, charmcraft will read the `worker/.jujuignore` file to determine what to leave out of the final charm.

### What's shared

The shared portions of each charm are within `worker/k8s` (except for the above mentioned exclusions).  This includes shared libraries from `worker/k8s/lib`, shared source from `worker/k8s/src`, shared python dependencies from `worker/k8s/requirements.txt`

### How to distinguish which charm code should engage

The charm can distinguish whether it's a `control-plane` or `worker` unit by using `self.is_worker` or `self.is_control_plane` by querying its metadata.

### Why two charms?

Much of the charm's behavior will be identical. They will employ many of the same relations, many of the same resources, configure the same snap, and use many of the same configuration options. One might therefore assume the two should be 1 charm. History with Charmed Kubernetes has proven that having 2 charms split between control-plane and worker has advantages when a relation is split across `requires` and `provides`.

### Why not use a charm library?

Sharing code between a charm library is a really reasonable idea, there are limitations that a charm library presents:

* limited to a single file
* PRs where the library changes doesn't reflect in the secondary charm
* updating a second charm isn't immediate
* must upload to charmhub, then download into the secondary charms

### How to use two charms in the same code base

In cases where the charms should diverge the behavior, use a runtime switch to make the decision

```python
if self.is_control_plane:
    # do control-plane only thing
    ...
# do more common things
...
if self.is_worker:
    # do worker only thing
    ...
```

## Integration tests

The functionality of the charms is verified through a set of [integration tests](../tests/integration/). While we officially only support deploying the charms using Terraform modules, the [Terraform Juju provider](https://registry.terraform.io/providers/juju/juju/latest/docs/resources/application#charm-1) currently only supports deploying charm applications from Charmhub, not from a local build.

This limitation poses a challenge for running integration tests against a pull request, where the charm under test has not yet been published. To work around this, we use a set of [local bundles](../tests/integration/data/) to deploy and test charms in these scenarios. These bundles are strictly for testing purposes and are not intended for use outside of the integration tests.

Lastly, to ensure that charm deployment via Terraform works correctly, we also run nightly tests. These tests fetch the latest charm from the `edge` channel and execute the integration tests against this version.
