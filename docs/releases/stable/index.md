# Creating a stable release

This document outlines the process for publishing a Canonical Kubernetes stable release.

## Background

### Repository Branching

This repositories used by Canonical Kubernetes has a branch scheme to provide a
consistent release experience. Any external or shared repositories are forked
into the `charmed-kubernetes` github organization and have the following branches:

* `main`: The primary development branch. Merges are made against this branch as they are approved.
* `release-1.xx`: The release branch. New major releases are branched from `main`.
* `release-1.xx`. Bugfix releases have specific commits PR'd to `release-1.xx` from a `bugfix_1.xx_<bugid>` branch.

Tags are used to mark releases on the `release-1.xx` branch.

### Feature Freeze

In the weeks prior to a stable release the team goes into a feature freeze. At this
time only bugfixes and concentration on resolving any other outstanding issues
will take place for the first week of this freeze.

The remaining tasks will still be completed at the time of feature freeze giving
Solutions QA a solid base to test from.

### Conflict resolution

At the time of the feature freeze, new `release-1.xx` branches are created to match
the default repo branch per the documentation below. During the feature freeze and
Solutions QA period, fixes which need to be applied to address CI or QA failures
(and only those specific fixes) are merged to the respective release branches.

## Prepare CI

### $stable++ release

It may feel early, but part of releasing the next stable version requires
preparing for the release that will follow. This requires opening tracks and
building relevant snaps and charms that will be used in the new `edge` channel.

To create tracks for the new release, run the following commands:
```
charmcraft create-track k8s 1.xx
charmcraft create-track k8s-worker 1.xx
```

## Preparing the release

### Create release branches for this repo

* **URL**: <https://github.com/canonical/k8s-operator/branches>
* **New Branch**: release-1.xx
* **source**:  main

We need to create a `release-1.xx` branch from `main`.
This will be our snapshot from which we test, fix, and subsequently
promote to the new release.

![Create Branch Dialog](create-branch-dialog.png)

### Pin snap channel in the release branches

The charms run the `k8s-snap` underneath, so to make sure the changes in the 
snap are going to be available in the charm, we need to make sure the correct
k8s snap channel is referenced. For that, the [snap_installation.yaml] file
needs to be updated either with the correct `channel`, or with the `revision` of the
snap.

- Example with `channel`:

```yaml
amd64:
- install-type: store
  name: k8s
  channel: 1.33-classic/stable
  classic: true
```

- Example with `revision`:

```yaml
amd64:
- install-type: store
  name: k8s
  revision: 2500
```

The [auto-update-snap-revision] job is also responsible for auto-updating the snap 
revision in the [snap_installation.yaml] file. This job is triggered on a schedule.

### Pin python versions of all python dependencies

In order to reproduce charm builds, we should pin the python dependencies of at least
the charm code.  The pinning should take place using a specific version of python
in order to ensure compatibility with the base os release. 

| base                     | python |
| ---                      | ---    |
| ubuntu@20.04 (**focal**) | 3.8    |
| ubuntu@22.04 (**jammy**) | 3.10   |
| ubuntu@24.04 (**noble**) | 3.12   |

Choose the environment based build-on base in `charms/worker/k8s/charmcraft.yaml`

In the following example, building on focal yields packages for python 3.8.

```yaml
  - build-on:
    - name: ubuntu
      channel: "20.04"
      architectures: [amd64]
```

Create a python 3.8 environment, and update the libraries.
Then create a PR to merge into the release branch.

The following commands are run in `k8s-operator/charms/worker/k8s`:

```shell
snap install astral-uv --classic
uv venv
uv venv -p 3.8
source .venv/bin/activate
uv sync
uv lock --upgrade
```
For further info on `uv`, see the Contributing guide in this repo.

### Build charms from the release branches

The [publish-charms] job is responsible for publishing the charms either to the
`latest/edge` OR `<release>/beta` (e.g. `1.33/beta`) channels, depending on the
branch that is updated. If a change is merged to the `main` branch, the charm will be
published to the `latest/edge` channel. If a change is merged to a release branch,
the charm will be published to the `<release>/beta` channel.

Raising a PR, passing the integration tests, and merging into the release
branch should publish the charm to the upstream `1.xx/beta` channel.

## Internal verification

### Make sure all tests are passing

It's assumed that tests pass on the release branch. This means that the CI for PRs
on the release branch should be green before they are merged. **Certain tests might
be skipped** because they rely on a specific cloud. The list of these
tests is as follows and needs to be updated when new tests are added.
We aim to remove this list in the future, and have all tests running in CI.

- [OpenStack tests]:
  These tests should be run manually on OpenStack by the individual responsible for
  the release. In order to run them, make sure you have an active Juju controller with
  an OpenStack cloud, and run:
  ```shell
  tox run -e integration -- -k test_openstack.py --apply-proxy --model test-openstack --keep-models
  ```

Also, make sure that all the [nightly tests] are passing.

### Run **validate-k8s-release-upgrade** job

**Job**: <https://jenkins.canonical.com/k8s-ps5/job/validate-k8s-release-upgrade/>

This validates the deployment using charms from the `$prev/stable` channel,
then performing an upgrade to `1.xx/beta`. The tests are parameterized to
run on:

* multiple series
* multiple architectures
* multiple clouds (aws/azure/gcp/vsphere)

### Promote charms to candidate

**Job**: <https://github.com/canonical/k8s-operator/actions/workflows/promote_charm.yaml>

Once we've made sure that the [publish-charms] job ran successfully for the latest
changes in the release branch, and the changes are available in `beta`, we can 
promote the charms to `candidate`. This can be done by running the [promote-charms] job
for the release branch:

![Promote to candidate](./beta-to-candidate.png)

### Notify Solutions QA

At the end of the first week and assuming all major blockers are resolved, the
release is passed over to Solutions QA (SQA) for sign-off. This is done by
[publishing a CI release](https://github.com/charmed-kubernetes/jenkins/releases/new)
with a new `1.xx` tag and informing SQA of that tag. The SQA team will have the
remaining week to test and file bugs so engineering can work towards getting
them resolved prior to stable release.

### CNCF Conformance

**Job**: <https://jenkins.canonical.com/k8s-ps5/job/conformance-cncf-k8s/>

Sync `canonical/k8s-conformance` main from upstream.

* <https://github.com/canonical/k8s-conformance>

Confirm passing results, then create a PR against the upstream `k8s-conformance`
repo.

Next, open an upstream PR:

* <https://github.com/cncf/k8s-conformance/pull/XXXXX>

> **Note**: CNCF requires a sign-off. After confirming results, issue a
`git commit --amend --signoff` on the branch prior to submitting the PR.

## Performing the release

### Document release notes

* Bugfixes
* Enhancements
* Known Limitations/Issues

### Promote charms to stable

**Job**: <https://github.com/canonical/k8s-operator/actions/workflows/promote_charm.yaml>

Run the workflow from a branch, select `release-1.xx`,

* Choose `Charm` - `all`
* Choose `Origin Channel`- `candidate`
* Choose `Destination Channel` - `stable`

![promote charm options](candidate-to-stable.png)

### Send announcement

Email announcement to k8s-crew with any relevant information.

# Fin

<!-- LINKS -->
[snap_installation.yaml]: ../../../charms/worker/k8s/templates/snap_installation.yaml
[auto-update-snap-revision]: ../../../.github/workflows/auto-update-snap-revision.yaml
[publish-charms]: ../../../.github/workflows/publish-charms.yaml
[OpenStack tests]: ../../../tests/integration/test_openstack.py
[nightly tests]: ../../../.github/workflows/nightly.yaml
