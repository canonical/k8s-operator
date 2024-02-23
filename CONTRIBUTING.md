# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

You can create an environment for development with `tox`:

```shell
tox devenv -e integration
source venv/bin/activate
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

Build all the charms in this git repository using:

```shell
charmcraft pack -p charms/worker
charmcraft pack -p charms/worker/k8s
```

### Formatting

This repo uses `isort` and `black` to format according to rules setup in `./charms/worker/k8s/pyproject.yaml`.  

Running the formatter is as easy as:
```shell
tox run -e format
```

If the github CI is complaining about invalid formatting, it could be due to an updated version of black, to fix locally, just run tox with a refreshed environment. The pip requirements will be refreshed locally on your machine and the formatter should be adjusted.

```shell
tox run -re format
```

### Linting

This repo uses a large assorment of static analysis tools configured with `./charms/worker/k8s/pyproject.yaml` to ensure that all source files maintain a similar code style and docs style.

Running the linter is as easy as:
```shell
tox run -e format
```

If the github CI is complaining about invalid formatting, it could be due to an updated version of black, to fix locally, just run tox with a refreshed environment. The pip requirements will be refreshed locally on your machine and the formatter should be adjusted.

```shell
tox run -re format
```


### Unit Testing
### Integration Testing
