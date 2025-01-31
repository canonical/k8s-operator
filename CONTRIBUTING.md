# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

You can create an environment for development with `tox`:

```shell
tox devenv -e integration
source venv/bin/activate
```

The development setup ships with tox3, you might want to install tox4:

```shell
pip install 'tox>=4,<5'
```

## Testing

This project uses `tox` for managing test environments. There are some pre-configured environments
that can be used for linting and formatting code when you're preparing contributions to the charm:

```shell
tox run -e format        # update your code according to linting rules
tox run -e lint          # code style
tox run -e unit          # unit tests
tox run -e integration   # integration tests
tox                      # runs 'lint', 'unit', 'static', and 'coverage-report' environments
```

* More on [Formatting]()
* More on [Linting]()
* More on [Unit Testing]()
* More on [Integration Testing]()

## Building the charms

In this repository, you'll find two machine charms.
The k8s charm handles the deployment of the control plane node, and the k8s-worker charm takes care of deploying worker nodes.
Build all the charms in this git repository using:

```shell
charmcraft pack -p charms/worker
charmcraft pack -p charms/worker/k8s
```

## Tox Environments

### Formatting

This repo uses `isort` and `black` to format according to rules setup in `./charms/worker/k8s/pyproject.yaml`.

Running the formatter is as easy as:

```shell
tox run -e format
```

If the github CI is complaining about invalid formatting, it could be due to an updated version of black. To fix locally, just run tox with a refreshed environment. The pip requirements will be refreshed locally on your machine and the formatter should be adjusted.

```shell
tox run -re format
```

### Linting

This repo uses static analysis tools configured with `./charms/worker/k8s/pyproject.yaml` to ensure that all source files maintain a similar code style and docs style.

Running the linter is as easy as:

```shell
tox run -e lint,static
```

If the github CI is complaining about invalid linting, it could be due to an updated version of one of the linter tools. To fix locally, just run tox with a refreshed environment. The pip requirements will be refreshed locally on your machine and the linters should be adjusted.

```shell
tox run -re lint,static
```

### Unit Testing

This repo uses `pytest` to execute unit tests against the charm code, and create a coverage report after the unit tests are completed. The unit tests are defined in `./charms/worker/k8s/tests/unit/`

Running the unit tests are as easy as:

```shell
tox run -e unit,coverage-report
```

Since the same charm code is executed on the worker and control-plane, in some unit test modules, we'll parameterize the tests to run against both the worker and control-plane to confirm both paths are tested. See `./charms/worker/k8s/tests/unit/test_base.py` for examples.

### Integration Testing

This repo uses `pytest` and `pytest-operator` to execute functional/integration tests against the charm files. The integration tests are defined in `./tests/integration`. Because this repo consists of two charms, the integration tests will build two charm files automatically without you doing anything. If you want to use specific charm files, just make sure the `.charm` files are in the top-level paths and the integration tests will find them if they are named appropriately (eg `./k8s-worker_*.charm` or `k8s_*.charm`). The charms are deployed according to the bundle defined in `./tests/integration/test-bundle.yaml`.

It's required you have a bootstrapped [juju machine controller](https://juju.is/docs/juju/manage-controllers) available. Usually, one prefers to have a controller available from their development machine to a supported cloud like `lxd` or `aws`. You can test if the controller is available by running:

```shell
juju status -m controller
```

You should see that there's a controller running on a cloud substrage like `aws` or `lxd` or some other cloud substrate that supports machines -- not a kubernetes substrate.

`pytest-operator` will create a new juju model and deploy a cluster into each model for every test module (eg `test_something.py`). For now, only one module is defined at `.tests/integration/test_k8s.py`. When the tests complete (successful or not), `pytest-operator` will clean up the models for you.

Running the integration tests are as easy as:

```shell
tox run -e integration-tests
```

Sometimes you will want to debug certain situations, and having the models torn down after a failed test prevents you from debugging. There are a few tools that make post-test debugging possible.

1) `juju-crashdump`: failed tests will create a juju-crashdump available in the toplevel with logs from each unit pulled out into an archive.
2) Running with extra arguments

Running the integration tests with extra arguments can be accomplished with

```shell
tox run -e integration-tests -- --positional --arguments
```

#### COS Integration

The COS integration tests are optional as these are slow/heavy tests. Currently, this suite only runs on LXD. If you are modifying something related to the COS integration, you can validate your changes through integration testing using the flag `--cos`. Also, when submitting a Pull Request with changes related to COS, you must include the `[COS]` tag in your Pull Request description. This will instruct GitHub Actions to execute the respective validation tests against your changes.

#### Useful arguments

`--keep-models`: Doesn't delete the model once the integration tests are finished
`--model`: Rerun the test with a given model name -- if it already exist, the integration tests will use it
`-k regex-pattern`: run a specific set of matching tests names ignore other passing tests
Remember that cloud costs could be incurred for every machine -- so be sure to clean up your models on clouds if you instruct pytest-operator to not clean up the models.

See [pytest-operator](https://github.com/charmed-kubernetes/pytest-operator/blob/main/docs/reference.md) and [pytest](https://docs.pytest.org/en/7.1.x/contents.html) for more documentation on `pytest` arguments
