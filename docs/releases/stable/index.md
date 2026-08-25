# Creating a stable release

This document outlines the process for publishing a Canonical Kubernetes stable release.

## Background

### Repository branching

This repositories used by Canonical Kubernetes has a branch scheme to provide a
consistent release experience. Any external or shared repositories are forked
into the `charmed-kubernetes` github organization and have the following branches:

* `main`: The primary development branch. Merges are made against this branch as they are approved.
* `release-1.xx`: The release branch. New major releases are branched from `main`.
* `release-1.xx`. Bug fix releases have specific commits committed to `release-1.xx` from a `bugfix_1.xx_<bugid>` branch via a pull-request.

Tags are used to mark releases on the `release-1.xx` branch.

### Feature freeze

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

## Prepare continuous integration

### Prepare next stable release

It's required to open tracks and building relevant snaps and 
charms that will be used in the new `edge` channel.

Create the track by following the documentation below:
* <https://discourse.charmhub.io/t/self-service-creation-of-tracks-now-available-to-charm-developers/10910>

An example request would be:

```bash
curl https://api.charmhub.io/v1/charm/k8s/tracks -X POST -H'Content-type: application/json' -H "$CHARMHUB_MACAROON_HEADER" -d '[{"name": "1.36"}]'
```

Ensure the track is created by querying the charm:

```bash
curl https://api.charmhub.io/v1/charm/k8s -H'Content-type: application/json' -H "$CHARMHUB_MACAROON_HEADER" | jq
```

**NOTE**: Make sure tracks are created for both `k8s` and `k8s-worker` charms.

## Preparing the release

### Create release branches for this repo

* **URL**: <https://github.com/canonical/k8s-operator/branches>
* **New Branch**: release-1.XX
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
  channel: 1.32-classic/stable
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

### Update the `literals.py` file

Look at the changes made to `charms/worker/k8s/src/literals.py` in the example commit below.
Make sure to adjust the changes according to the release you're making. This commit was
in preparation of the `1.35` release.

* <https://github.com/canonical/k8s-operator/pull/830/changes>

### Build charms from the release branches

The [publish-charms] job is responsible for publishing the charms either to the
`latest/edge` OR `<release>/beta` (e.g. `1.32/beta`) channels, depending on the
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

### Promote charms to candidate

**Job**: <https://github.com/canonical/k8s-operator/actions/workflows/promote_charm.yaml>

Once we've made sure that the [publish-charms] job ran successfully for the latest
changes in the release branch, and the changes are available in `beta`, we can
promote the charms to `candidate`. This can be done by running the [promote-charms] job
for the release branch:

![Promote to candidate](./beta-to-candidate.png)

### Notify quality team

At the end of the first week and assuming all major blockers are resolved, the
release is passed over to Solutions QA (SQA) for sign-off. This is done by
[publishing a CI release](https://github.com/charmed-kubernetes/jenkins/releases/new)
with a new `1.xx` tag and informing SQA of that tag. The SQA team will have the
remaining week to test and file bugs so engineering can work towards getting
them resolved prior to stable release.

## Performing the release

### Document release notes

* Bug fixes
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
