# Migrating integration tests from python-libjuju to Jubilant

This document is the complete, self-contained plan for migrating the integration
test suite under `tests/integration/` from **python-libjuju** (`import juju`) +
**pytest-operator** (`OpsTest`) to **Jubilant** (`import jubilant`).

It is written so that acting on it is mechanical. Read sections 1–4 once for the
mental model, then use sections 5–8 as the per-symbol / per-file reference while
editing, and section 9 as the gotcha checklist before you open the PR.

> Status of dependencies: `jubilant>=1.10.0` is **already** declared in both
> `pyproject.toml` and `charms/worker/k8s/pyproject.toml`. python-libjuju (`juju`),
> `pytest-operator`, and `pytest-asyncio` are still in the `integration`
> dependency group and must be removed at the end of the migration.

---

## 0. STATUS: COMPLETED — implementation notes & corrections

The migration described below has been implemented. A few facts were verified
against the **installed jubilant 1.10.0 source** and the live `juju` 3.6 CLI and
differ from earlier guesses in this document; the code follows the verified facts:

- **`kubernetes` is now an explicit `integration` dependency.** It was previously
  pulled in *transitively by python-libjuju* (`juju` depends on `kubernetes`).
  Removing `juju` dropped it, which would have broken every test that imports
  `kubernetes.*`. Added `kubernetes` to the `integration` group; `uv lock`
  resolves it (36.x).
- **`Status.model` has no `uuid`** (only `name/type/controller/cloud/version/region`).
  Model UUID is obtained via `juju.show_model().model_uuid` (used by `cloud_profile`).
- **`ModelInfo` has no `owner`.** The cross-model offer qualifier is obtained via
  `juju whoami --format json` → `["user"]` (`_model_owner` in `conftest.py`).
- **`cloud_type`** is derived from `juju.status().model.cloud` + `juju clouds --all
  --format json` (cloud → type), not `show-controller`/`show-cloud`.
- **`cloud_arch`** parses the `hardware` string of the `controller` model's machines.
- **COS k8s cloud teardown uses `juju remove-k8s`** (not `remove-cloud`); cloud is
  registered with `juju add-k8s <name> --client --controller <c>` and `KUBECONFIG`.
- **`remove-offer`** needs `--force -y`; **`remove-saas`** does not prompt.
- **`exec`/`run`/`cli` raise** (`TaskError`/`CLIError`) on failure (verified). Places
  that tolerate failure use `contextlib.suppress(jubilant.TaskError)` (cleanup
  `kubectl delete`) or catch `TaskError` to read `e.task.stdout` (the openssl SAN
  check in `test_openstack.py`).
- **`series: resolute` (Ubuntu 26.04)** is unknown to juju ≤3.6's
  `get_series_version`, so a local `_SERIES_TO_VERSION` map in `helpers.py`
  (incl. `resolute -> 26.04`) replaces `juju.utils.get_series_version`. The
  `juju.url.URL` machinery is replaced by plain `ch:<arch>/<name>` /
  `ch:<arch>/<series>/<name>` string handling.
- **Approach chosen: bare `jubilant` + hand-written `conftest.py` fixtures**
  (strategy A), to keep the CLI surface the CI injects (`--keep-models`, and for
  nightly `--model`/`--no-deploy`). `conftest.py` re-adds `--model`,
  `--keep-models`, `--no-deploy`, `--model-config`, `--cloud`, `--controller`.
- **`abort_on_fail`** is registered as a no-op marker in `[tool.pytest.ini_options]`
  (it was provided by pytest-operator); `--exitfirst` in `tox.ini` provides
  fail-fast. The `--crash-dump*` flags were removed; failure logs are captured via
  `juju.debug_log()` in the `juju` fixture.
- **CI invocation confirmed** (operator-workflows, LXD path):
  `tox -e integration -- --keep-models <series> <-k module> <args> [--lxd-containers]`;
  nightly adds `--model <name> --no-deploy`. All injected options are defined in
  `conftest.py`.

Verified locally (no controller needed): `ruff check` + `ruff format --check` pass,
all modules `py_compile`, and `pytest --collect-only` collects all 32 tests with no
import/marker/option errors. Integration tests themselves require a Juju controller
and were not run.

---

---

## 1. The fundamental shift (read this first)

This is **not** a find-and-replace migration. Two independent things are being
replaced at once:

| Today | After |
|---|---|
| `python-libjuju` — async, connects to the Juju API over websockets | `jubilant` — **synchronous**, shells out to the `juju` CLI and parses output |
| `pytest-operator` — the `ops_test` fixture, model lifecycle, charm building, bundle rendering, CLI options | **nothing equivalent** — Jubilant ships no pytest fixtures; we provide our own in `conftest.py` |

Concrete consequences that touch almost every file:

1. **All `async def` / `await` disappears.** Tests and fixtures become plain
   `def`. `pytest_asyncio.fixture` → `pytest.fixture`. `asyncio.gather(...)`
   parallelism is **lost** and must become sequential loops (or `concurrent.futures`
   threads if the wall-clock cost matters — see §9.6).
2. **There is no background model state.** python-libjuju keeps
   `app.units[0].workload_status` live via websockets. Jubilant has no live
   objects: you call `juju.status()` and read a snapshot. Every access to
   `app.units`, `unit.workload_status`, `unit.public_address`, etc. becomes a
   field on the `Status` snapshot.
3. **`ops_test` does not exist.** Everything reached through `ops_test`
   (model creation, `model_context`, `fast_forward`, `build_charm`,
   `async_render_bundles`, `run`, `juju`, `tmp_path`, `cloud_name`, `track_model`,
   `forget_model`, `add_k8s`, `ModelKeep`, `read_model_config`) must be
   reimplemented in `conftest.py` or replaced by a `jubilant.Juju` method /
   `juju.cli(...)`.
4. **Errors raise instead of returning codes.** `juju.exec`, `juju.run`, and
   `juju.cli` raise (`TaskError` / `CLIError`) on failure. Manual
   `assert result.results["return-code"] == 0` checks become unnecessary — and
   places that *intentionally tolerate* a non-zero exit must now wrap calls in
   `try/except` (see §9.2).

---

## 2. Scope: files affected

Every file under `tests/integration/` that imports `juju` or uses `ops_test`:

| File | What's in it | Migration weight |
|---|---|---|
| `conftest.py` | model lifecycle, fixtures, cloud/proxy/profile setup, COS multi-model, CLI options | **Heavy** — the crux of the migration |
| `helpers.py` | `Bundle`/`Charm`/`Markings`, `get_leader`, `ready_nodes`, `get_rsc`, `wait_pod_phase`, `get_kubeconfig`, `cloud_arch`, `cloud_type`, URL/series utils | **Heavy** |
| `storage.py` | `exec_storage_class` (uses `unit.run`) | Medium |
| `test_k8s.py` | config get/set, unit run, add/remove unit, destroy, attach_resource, leader | Medium |
| `test_upgrade.py` | `refresh`, `run_action`, custom idle wait, `JujuUnitError` | Medium |
| `test_cos.py` | model name, async helpers | Light (model.name only) |
| `test_etcd.py` / `test_charmed_etcd.py` | `unit.run`, `safe_data["ports"]`, `public_address`, `add_unit`, `list_secrets`, `remove_relation`, deploy/integrate | Medium |
| `test_ceph.py` | `app.charm_name`, `workload_status_message`, `wait_for_idle`, `fast_forward` | Light/Medium |
| `test_smoke.py` | config preserve, leader, ready_nodes | Light |
| `test_openstack.py` | `set_config`, `run_action`, `get_public_address`, `unit.run` | Medium |
| `test_registry.py` | `set_config`, `run_action`, `unit.run`, `get_public_address` | Medium |
| `test_dualstack.py` / `test_ipv6_only.py` | `ready_nodes`, leader only | Light |
| `terraform/setup.py` | `juju.controller.Controller` for TF env vars | Standalone — see §8.11 |
| `grafana.py` / `prometheus.py` | `async def` methods, no juju usage | Light — drop `async` |
| `cos_substrate.py` / `lxd_substrate.py` | no python-libjuju usage (lxc/pylxd) | None |

CI coupling that must be addressed: `tox.ini` (`[testenv:integration]`) and
`.github/workflows/integration_test.yaml` (uses the
`canonical/operator-workflows` reusable workflow). See §7 and §9.1.

---

## 3. Dependency and tooling changes

### `pyproject.toml` (root) and `charms/worker/k8s/pyproject.toml`

`jubilant>=1.10.0` is already in `dependencies`. The `integration` dependency
group currently is:

```toml
integration = [
    "async-lru",
    "juju",            # REMOVE (python-libjuju)
    "pydantic",
    "pylxd",
    "pytest",
    "pytest-asyncio",  # REMOVE
    "pytest-operator", # REMOVE
    "pytest-sugar",
    "tenacity",
    "requests <2.34",
]
```

After migration:

```toml
integration = [
    "pydantic",
    "pylxd",
    "pytest",
    "pytest-sugar",
    "tenacity",
    "requests <2.34",
    # jubilant is already in [project].dependencies
    # Optionally: "pytest-jubilant>=2,<3"  (see §6 — NOT recommended for this repo)
]
```

Notes:
- `async-lru` is only used for the `@alru_cache` on `cloud_arch`/`cloud_type`.
  Once those are sync, switch to `functools.lru_cache` and drop `async-lru`.
- Keep `jubilant` pinned consistently in both `pyproject.toml` files; update
  `uv.lock` (`uv lock`) after editing.
- Do **not** remove `pytest-asyncio` until the last file loses `async def`,
  otherwise collection fails.

### `tox.ini`

The `[testenv:integration]` command passes pytest-operator-only flags:

```
--crash-dump=on-failure
--crash-dump-args='-j snap.k8s.* --as-root --addons-file ... -a k8s-inspect'
```

`--crash-dump*` is provided by **pytest-operator** (it shells out to
`juju-crashdump`). Jubilant has no crash-dump integration. Options (§9.7):
- Drop the flags and capture logs in a fixture via `juju.debug_log()` on failure.
- Or call `juju-crashdump` directly from a teardown fixture / `--juju-dump-logs`
  if you adopt `pytest-jubilant`.

The `deploy-terraform` env (`tests/integration/terraform/setup.py`) is a separate
concern; see §8.11.

---

## 4. Mental-model differences that bite

- **Snapshots, not live objects.** Call `status = juju.status()` once per logical
  step and read from it. Re-fetch after any mutating operation.
- **Wait is explicit and callable-driven.** `wait_for_idle(status="active")`
  becomes `juju.wait(jubilant.all_active)`. The ready/error functions take the
  `Status` snapshot and return `bool`. Add `error=jubilant.any_error` to fail fast.
- **`wait` default timeout is 180 s** (`Juju.wait_timeout`), and it requires
  `successes=3` consecutive good polls (`delay=1.0s`). python-libjuju's
  `wait_for_idle` defaulted differently and used `idle_period`. Always pass an
  explicit `timeout=` for long operations.
- **`wait` raises `TimeoutError` on timeout** and `jubilant.WaitError` if the
  `error` callable returns `True`. (python-libjuju raised
  `asyncio.TimeoutError` / `JujuUnitError`.)
- **Series is gone; use `base`.** `juju.deploy(..., base="ubuntu@22.04")`. There
  is **no** `series=` parameter anywhere in Jubilant.
- **`config()` returns plain values**, not metadata dicts (big behavioral change —
  see §5 and §9.3).

---

## 5. API mapping: python-libjuju → Jubilant

`m` = `juju.model.Model`, `app` = `Application`, `u` = `Unit`. On the Jubilant
side, `juju` is a `jubilant.Juju` instance and `st = juju.status()`.

### Model / deploy / lifecycle

| python-libjuju | Jubilant |
|---|---|
| `await m.deploy(url, channel=, series=, application_name=, constraints=, config=, trust=, num_units=)` | `juju.deploy(charm, app=None, *, channel=, base="ubuntu@22.04", constraints={...}, config={...}, trust=, num_units=, to=, resources=, revision=, overlays=)` — **`series=`→`base=`**, `application_name=`→positional `app`, `constraints` is a **mapping** not a string |
| `await m.deploy(bundle_yaml, trust=...)` | `juju.deploy(bundle_path, trust=..., overlays=[...])` |
| `await m.integrate(a, b)` | `juju.integrate(a, b)` |
| `await m.wait_for_idle(apps=[...], status="active", timeout=T, idle_period=, raise_on_error=)` | `juju.wait(lambda s: jubilant.all_active(s, *apps), timeout=T)` — see §5 "wait" below |
| `await m.remove_application(name)` | `juju.remove_application(name, destroy_storage=, force=)` |
| `await m.block_until(predicate, timeout=)` | poll manually or `juju.wait(lambda s: predicate, ...)`; for "app present/absent" build a predicate against `s.apps` |
| `await m.list_secrets(show_secrets=True)` | `juju.secrets()` → `list[Secret]`; reveal with `juju.show_secret(uri, reveal=True)` (see §8.6) |
| `await m.set_config({...})` (model) | `juju.model_config({...})` |
| `m.applications` | `st.apps` (`dict[str, AppStatus]`) |
| `m.applications[name]` | `st.apps[name]` |
| `m.name` | `juju.model` (the model name string) |
| `m.uuid` | `juju.status().model.uuid` (`st.model.uuid`) |
| `m.info.owner_tag` (+ `untag`) | `juju.show_model().owner` (no tag prefix to strip — see §8.9) |
| `await m.get_controller()` / `controller.cloud()` | parse `juju.cli('show-controller', '--format=json')` / `show-cloud` (see §8.7) |
| `m.remove_saas(name)` | `juju.cli('remove-saas', name)` — **no dedicated method** |
| `m.remove_offer(name, force=True)` | `juju.cli('remove-offer', '--force', '-y', name)` — **no dedicated method** |

### Application

| python-libjuju | Jubilant |
|---|---|
| `app.units` | `st.apps[name].units` (`dict[str, UnitStatus]`) or `st.get_units(name)` |
| `len(app.units)` | `len(st.get_units(name))` |
| `app.name` | the dict key / the app name string you already hold |
| `app.charm_name` | `st.apps[name].charm_name` |
| `await app.get_config()` | `juju.config(name)` → `dict[str, value]` (**plain values**, §9.3) |
| `await app.set_config({...})` | `juju.config(name, {...})` |
| `await app.reset_config([keys])` | `juju.config(name, reset=[keys])` |
| `await app.add_unit(count=n)` | `juju.add_unit(name, num_units=n)` |
| `await app.refresh(path=, resources=, channel=, revision=)` | `juju.refresh(name, path=, resources=, channel=, revision=, trust=)` |
| `app.attach_resource("res", path, fileobj)` | `juju.cli('attach-resource', name, f'res={path}')` — **no dedicated method** (§8.10) |
| `await app.destroy_relation(local, remote, block_until_done=True)` | `juju.remove_relation(local_endpoint, remote_endpoint)` then `juju.wait(...)` |
| `await app.remove_relation(a_ep, b_ep)` | `juju.remove_relation(a_ep, b_ep)` |

### Unit

| python-libjuju | Jubilant |
|---|---|
| `u.name` | the unit dict key (e.g. `"k8s/0"`) |
| `u.workload_status` | `st.apps[a].units[uname].workload_status.current` (or `.is_active`) |
| `u.workload_status_message` | `st.apps[a].units[uname].workload_status.message` |
| `u.public_address` / `await u.get_public_address()` | `st.apps[a].units[uname].public_address` |
| `u.safe_data["ports"]` | `st.apps[a].units[uname].open_ports` → `list[str]` like `"2379/tcp"` (parse the number, §9.4) |
| `await u.is_leader_from_status()` | `st.apps[a].units[uname].leader` (bool) |
| `await u.destroy()` | `juju.remove_unit(uname)` |
| `await u.run(cmd)` then `await ev.wait()` | `juju.exec(cmd, unit=uname)` → `Task` (raises on failure, §9.2) |
| `await u.run_action(name, **params)` then `await act.wait()` | `juju.run(uname, name, {params})` → `Task` (raises on failure) |

### Actions / exec results (`Task`)

python-libjuju `action.results` is a dict including `return-code`, `stdout`,
`stderr`, plus charm action outputs. Jubilant splits these onto the `Task`:

| python-libjuju | Jubilant `Task` |
|---|---|
| `result.results["return-code"]` | `task.return_code` |
| `result.results["stdout"]` | `task.stdout` |
| `result.results["stderr"]` | `task.stderr` |
| `result.results["<action-output-key>"]` (e.g. `kubeconfig`, `admin-password`, `proxied-endpoints`) | `task.results["<key>"]` |
| `result.status == "completed"` | `task.status == "completed"` or `task.success` (`completed` **and** `return_code == 0`) |

`Task` fields: `id, status, return_code, stdout, stderr, message, log, results`.
`task.success` is `True` iff `status == "completed"` and `return_code == 0`.
`task.raise_on_failure()` raises `jubilant.TaskError` when not successful — but
`exec`/`run` already call this for you, so manual checks are redundant.

### Waiting

```python
# python-libjuju
await m.wait_for_idle(apps=["k8s", "k8s-worker"], status="active", timeout=t*60)
```
```python
# Jubilant
juju.wait(
    lambda s: jubilant.all_active(s, "k8s", "k8s-worker"),
    error=jubilant.any_error,
    timeout=t * 60,
)
```

Helper callables: `all_active, all_blocked, all_waiting, all_maintenance,
all_error, all_agents_idle` and `any_active, any_blocked, any_waiting,
any_maintenance, any_error`. Signature: `all_active(status, *apps) -> bool`
(no apps ⇒ all apps in the model; returns `False` if a named app is missing).

- `wait_for_idle(status="active")` (all apps) → `juju.wait(jubilant.all_active)`.
- `wait_for_idle(apps=[...], status="blocked")` →
  `juju.wait(lambda s: jubilant.all_blocked(s, *apps))`.
- `raise_on_error=False` has **no direct equivalent**; simply omit
  `error=jubilant.any_error` so errored units don't abort the wait.
- `idle_period=30` → Jubilant has no `idle_period`; tune via `successes=` /
  `delay=` (e.g. `successes=30, delay=1.0` ≈ 30 s of stability) or combine with
  `jubilant.all_agents_idle`.

### Status snapshot object shapes (for reference)

- `Status.apps: dict[str, AppStatus]`, `Status.machines: dict[str, MachineStatus]`,
  `Status.model: ModelStatus` (has `name`, `uuid`, ...).
- `Status.get_units(app) -> dict[str, UnitStatus]` (includes subordinates).
- `AppStatus`: `charm_name`, `charm_channel`, `base`, `units`, `relations`,
  `app_status: StatusInfo`, plus `is_active/is_blocked/is_error/is_waiting/is_maintenance`.
- `UnitStatus`: `workload_status: StatusInfo`, `juju_status: StatusInfo`,
  `leader: bool`, `machine`, `open_ports: list[str]`, `public_address`, `address`,
  `subordinates`, plus `is_active/...` shortcuts.
- `MachineStatus`: `hardware: str` (e.g. `"arch=amd64 cores=2 mem=4096M ..."`),
  `base`, `instance_id`, `ip_addresses`, ... (parse `hardware` for arch, §8.7).
- `StatusInfo`: `current, message, reason, since, version, life`.

---

## 6. Replacing the `ops_test` fixture

Jubilant ships **no** pytest fixtures. Two strategies:

**(A) Bare `jubilant` + hand-written `conftest.py` fixtures — RECOMMENDED here.**
Gives full control over the things this repo needs (LXD profiles, proxy config,
cloud detection, multi-model COS, custom CLI options that CI injects).

**(B) `pytest-jubilant` plugin.** Provides `juju` (module-scoped, temp model) and
`juju_factory` fixtures, `@pytest.mark.juju_setup`/`juju_teardown` markers, and CLI
options `--juju-model`, `--juju-controller`, `--juju-cloud`, `--no-juju-setup`,
`--no-juju-teardown`, `--juju-switch`, `--juju-dump-logs`.

> **Why (A) for this repo:** `pytest-jubilant`'s option names
> (`--juju-model`, `--no-juju-setup`, `--juju-controller`) do **not** match the
> flags the existing CI passes (`--model`, `--keep-models`, `--cloud`,
> `--no-deploy`). Adopting (B) would require also changing the CI workflow inputs
> and our deploy-once/`--no-deploy` model-reuse pattern. (A) lets us keep the
> existing CLI surface. See §9.1.

### The core `juju` fixture (strategy A)

```python
import contextlib
import secrets
import jubilant
import pytest


def pytest_addoption(parser: pytest.Parser):
    # ... keep existing custom options (see §7) ...
    # Re-add options previously provided by pytest-operator that the code/CI use:
    parser.addoption("--model", default=None, help="Use an existing model instead of a temp one")
    parser.addoption("--keep-models", action="store_true", default=False,
                     help="Keep temporarily-created models")
    parser.addoption("--no-deploy", action="store_true", default=False,
                     help="Reuse an already-deployed model; skip deployment")
    parser.addoption("--model-config", default=None, help="Path to a model-config YAML")
    parser.addoption("--cloud", default=None, help="Cloud (and optional region) to deploy to")
    parser.addoption("--controller", default=None, help="Juju controller to use")


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest) -> jubilant.Juju:
    """Module-scoped Juju instance against a temp model (or an existing one)."""
    keep = request.config.getoption("--keep-models")
    existing = request.config.getoption("--model")
    controller = request.config.getoption("--controller")
    cloud = request.config.getoption("--cloud")

    if existing:
        # Reuse an existing model (CI / --no-deploy path).
        juju = jubilant.Juju(model=existing)
        yield juju
        return

    config = _read_model_config(request)  # see §7 read_model_config
    with jubilant.temp_model(keep=keep, controller=controller,
                             cloud=cloud, config=config) as juju:
        yield juju
```

`jubilant.temp_model(keep=False, controller=None, cloud=None, config=None,
credential=None)` creates a `jubilant-<8 hex>` model and destroys it on exit
(`destroy-model --no-prompt --destroy-storage --force`, 10-min timeout) unless
`keep=True`.

For a **session/tmp dir** previously from `ops_test.tmp_path`, use pytest's
built-in `tmp_path`/`tmp_path_factory` fixtures.

---

## 7. Rewriting `conftest.py` (the heavy lift)

Inventory of what the current `conftest.py` pulls from `ops_test` and how each maps:

| `ops_test.*` / option | Replacement |
|---|---|
| `ops_test.model` | the `juju` fixture (`jubilant.Juju`) |
| `ops_test.model.name` | `juju.model` |
| `ops_test.model.uuid` | `juju.status().model.uuid` |
| `ops_test.model.set_config({...})` | `juju.model_config({...})` |
| `ops_test.model.get_controller()` / `controller.get_model("controller")` | `jubilant.Juju(model="controller")` (the controller model) for `cloud_arch`; `show-controller`/`show-cloud` for `cloud_type` (§8.7) |
| `ops_test.track_model(name, model_name=, credential_name=, config=, cloud_name=, keep=)` | `juju.add_model(name, cloud=..., config=..., credential=...)` or a second `jubilant.Juju` + `temp_model` (§8.8) |
| `ops_test.model_context(name)` | hold separate `jubilant.Juju(model=name)` instances; there is no implicit "current model" switching needed — each `Juju` targets its own model (§8.8) |
| `ops_test.models` / `ops_test.ModelKeep` | manage your own dict of `Juju` objects; `keep=` on `temp_model` |
| `ops_test.forget_model(name, timeout=, allow_failure=)` | `juju.destroy_model(name, destroy_storage=True, force=True, timeout=...)` |
| `ops_test.fast_forward(interval)` | `fast_forward(juju, interval)` context manager (§8.1) |
| `ops_test.add_k8s(kubeconfig=, skip_storage=)` | `juju.cli('add-k8s', name, ...)` with `KUBECONFIG` (§8.5) |
| `ops_test.Bundle(name, channel)` / `ops_test.async_render_bundles(*b)` | render bundle files yourself (already done by the `Bundle` dataclass) and `juju.deploy(path, overlays=[...])`, or shell `juju deploy` via `juju.cli` (§8.4) |
| `ops_test.run(*cmd)` / `ops_test.juju(*args)` | `juju.cli(*args)` (raises `CLIError`; returns stdout) |
| `ops_test.build_charm(path, return_all=True)` | **no equivalent** — build out-of-band and pass `--charm-file`, or shell `charmcraft pack` (§8.3) |
| `ops_test.read_model_config(path)` | small local helper: `yaml.safe_load(Path(path).read_text())` |
| `ops_test.cloud_name` | `--cloud` option value, or parse `show-controller` |
| `ops_test.request` | the `request` fixture directly |
| `ops_test.tmp_path` | pytest `tmp_path` / `tmp_path_factory` |
| `config.option.no_deploy` | **must re-add** `--no-deploy` addoption (was pytest-operator's) |
| `config.option.model_config` | **must re-add** `--model-config` addoption |
| `config.option.series` / `arch` / `charm_files` / `apply_proxy` / `timeout` / `snap_installation_resource` / `metrics_agent_charm` | already defined locally in `pytest_addoption` — keep as-is |

> **Critical:** `--no-deploy` and `--model-config` are read via
> `ops_test.request.config.option.no_deploy` / `.model_config` but are **defined by
> pytest-operator**, not by this repo. Removing pytest-operator removes their
> definitions → pytest collection fails with "unrecognized arguments" the moment
> CI passes them. They must be re-added to `pytest_addoption` (shown in §6).

### `deploy_model` / `kubernetes_cluster`

Current flow: `track_model` → `model_context` → `cloud_profile` → `fast_forward` →
`bundle.render` → `the_model.deploy(bundle_yaml, trust=...)` → `wait_for_idle`.

Rewritten (sync) shape:

```python
@contextlib.contextmanager
def deploy_model(request, juju: jubilant.Juju, bundle: Bundle):
    at_least_60 = max(60, request.config.option.timeout)
    cloud_profile(request, juju)              # was cloud_profile(ops_test)
    with fast_forward(juju, ONE_MIN):
        bundle_path = bundle.render(request.config._tmp_path_factory.mktemp("bundles"))
        juju.deploy(bundle_path, trust=bundle.needs_trust)
        juju.wait(
            lambda s: jubilant.all_active(s, *bundle.applications),
            timeout=at_least_60 * 60,
        )
    yield juju


@pytest.fixture(scope="module")
def kubernetes_cluster(request, juju: jubilant.Juju):
    bundle, markings = Bundle.create(request, juju)   # now sync
    if bundle.is_deployed(juju):                       # now sync (status-based)
        yield juju
        return
    if request.config.option.no_deploy:
        pytest.skip("Skipping because of --no-deploy")
    if request.config.option.apply_proxy:
        cloud_proxied(request, juju)
    bundle.apply_marking(request, juju, markings)
    with deploy_model(request, juju, bundle):
        yield juju
```

Note: `cloud_arch`, `cloud_type`, `cloud_profile`, `cloud_proxied`,
`Bundle.create`, `Bundle.is_deployed`, `Bundle.apply_marking`,
`discover_charm_files` all lose `async`. The `@alru_cache` on
`cloud_arch`/`cloud_type` becomes `@functools.lru_cache` (the `Juju` instance is
hashable by identity; or cache on `juju.model`).

### COS multi-model fixtures

`cos_model`, `_cos_lite_installed`, `traefik_url`, `grafana_password`,
`related_grafana`, `related_prometheus` use a **second model** ("cos") on a
**separate k8s cloud**, plus cross-model offers. This is the most intricate part:

- Replace `ops_test.add_k8s(...)` + `track_model("cos", cloud_name=...)` with
  `juju.cli('add-k8s', cloud_name, ...)` then a dedicated
  `cos_juju = jubilant.Juju(model="cos"); cos_juju.add_model("cos", cloud=cloud_name)`
  (§8.5, §8.8).
- `with ops_test.model_context("main") as model: ...` / `model_context("cos")`
  become operations on the respective `jubilant.Juju` objects (`main_juju`,
  `cos_juju`). There's no global "current model" — each call is explicit.
- `model.integrate(metrics_agent, f"{owner}/{cos_name}.grafana-dashboards")` →
  `main_juju.integrate(metrics_agent, f"{owner}/{cos_name}.grafana-dashboards")`
  (consume/offer cross-model SAAS still works through `integrate` with the
  `owner/model.offer` syntax, same as the CLI).
- `model.remove_saas(...)` / `model.remove_offer(...)` → `juju.cli('remove-saas', ...)`
  / `juju.cli('remove-offer', '--force', '-y', ...)`.
- The COS-lite deploy currently shells `juju deploy ... --overlay=...` through
  `ops_test.run`. Keep that, but via `cos_juju.cli('deploy', bundle, '--trust',
  '--overlay', overlay)` (note `include_model=True` inserts `-m cos` automatically).
- `cos_model.block_until(lambda: all(app in apps ...))` → poll
  `cos_juju.status().apps` in a small retry loop, or `cos_juju.wait(...)` with a
  predicate over `s.apps`.

This section is where the most careful manual work and testing is required.

---

## 8. Specific reimplementations (copy-paste-ready patterns)

### 8.1 `fast_forward`

```python
import contextlib

@contextlib.contextmanager
def fast_forward(juju: jubilant.Juju, interval: str = "1m"):
    old = juju.model_config()["update-status-hook-interval"]
    juju.model_config({"update-status-hook-interval": interval})
    try:
        yield
    finally:
        juju.model_config({"update-status-hook-interval": old})
```

### 8.2 `get_leader` (helpers.py)

```python
def get_leader(juju: jubilant.Juju, app: str) -> str:
    """Return the leader unit name (e.g. 'k8s/0')."""
    for name, unit in juju.status().get_units(app).items():
        if unit.leader:
            return name
    raise ValueError("No leader found")
```

Note the signature change: it now takes `(juju, app_name)` and returns the **unit
name string**, not an index. Update all callers (they currently do
`leader_idx = await get_leader(k8s); leader = k8s.units[leader_idx]`).

### 8.3 Charm building / discovery (`build_charm`)

`ops_test.build_charm` packs charms on the fly. Jubilant has nothing for this.
In CI charms are pre-built and passed via the existing `--charm-file` option, so
the **discovery** path in `Charm.resolve` is unchanged. Only the **fallback**
that calls `await ops_test.build_charm(self.path, return_all=True)` must change:

```python
import subprocess
# fallback inside Charm.resolve (sync):
subprocess.run(["charmcraft", "pack", "-p", str(self.path)], check=True)
potentials = self.path.glob(f"{self.name}_*.charm")
self._charmfile = _narrow(potentials)
```

Recommended: require pre-built charms (always pass `--charm-file`) and make the
build fallback raise a clear error, matching the upstream guidance to "decouple
packing from testing."

### 8.4 Bundles

The `Bundle`/`Charm`/`Markings` machinery in `helpers.py` is mostly pure
data/YAML manipulation and stays — but:
- Drop `from juju.url import URL` and `juju.utils`. Replace `URL.parse` /
  `URL.with_series` / the `url_representer` with plain string handling of charm
  references (name, channel, base/arch are already strings in the bundle YAML).
- Replace `juju.utils.get_series_version("jammy") -> "22.04"` and
  `get_version_series("22.04") -> "jammy"` with a tiny local mapping if still
  needed. Prefer migrating bundle entries to `base: ubuntu@22.04` and dropping
  series entirely.
- Deploy the rendered bundle with `juju.deploy(rendered_path, trust=...,
  overlays=[...])`.

### 8.5 `add_k8s` (COS)

```python
# was: await ops_test.add_k8s(kubeconfig=config, skip_storage=False)
import os
# add-k8s reads the cluster from KUBECONFIG; it is a client/controller-level
# command (not model-scoped), so use include_model=False.
os.environ["KUBECONFIG"] = str(cos_substrate)   # path to the kubeconfig file
juju.cli("add-k8s", k8s_cloud_name, "--client", "--controller", controller_name,
         include_model=False)
```

`jubilant.Juju.cli(*args, include_model=True, stdin=None)` returns stdout and
raises `CLIError`. **There is no `env=` parameter** — set `os.environ`
(e.g. `KUBECONFIG`) before the call, or fall back to `subprocess.run` if you need
tighter control over the environment. Confirm parity with
`ops_test.add_k8s(skip_storage=False)` (storage handling / region flags).

### 8.6 Secrets (`test_charmed_etcd.py`)

```python
# was: secrets = await m.list_secrets(show_secrets=True)
#      s.owner_tag == "application-charmed-etcd"; s.value.data["tls-ca"]
secrets = juju.secrets()                       # list[Secret] (metadata only)
for s in secrets:
    if s.owner == "charmed-etcd":              # owner is the app name, no "application-" tag
        revealed = juju.show_secret(s.uri, reveal=True)  # RevealedSecret
        data = revealed.content                # dict[str, str]
        if "tls-ca" in data:
            ...
```
Verify the exact field names on `Secret`/`RevealedSecret` against the installed
jubilant version (`owner`, `uri`, `content`). This module is currently
`pytest.skip`-ped at import, so it's low priority but still must compile.

### 8.7 `cloud_arch` and `cloud_type` (helpers.py)

```python
import functools

@functools.lru_cache
def cloud_arch(juju: jubilant.Juju) -> str:
    # controller machines live in the 'controller' model
    ctrl = jubilant.Juju(model="controller")
    machines = ctrl.status().machines
    arches = set()
    for m in machines.values():
        # hardware is a string: "arch=amd64 cores=2 mem=4096M ..."
        for kv in m.hardware.split():
            k, _, v = kv.partition("=")
            if k == "arch":
                arches.add(v)
    return arches.pop()


@functools.lru_cache
def cloud_type(request, juju: jubilant.Juju) -> tuple[str, bool]:
    import json
    out = juju.cli("show-controller", "--format=json", include_model=False)
    info = next(iter(json.loads(out).values()))
    cloud = info["details"]["cloud"]
    cloud_out = juju.cli("show-cloud", cloud, "--format=json", include_model=False)
    _type = json.loads(cloud_out)["type"]
    vms = True
    if _type == "lxd":
        vms = not request.config.getoption("--lxd-containers")
    return _type, vms
```

Validate the `show-controller` / `show-cloud` JSON shape on your Juju version.
`MachineStatus.hardware` being a single string is the key gotcha vs.
`safe_data["hardware-characteristics"]["arch"]`.

### 8.8 Multiple models

python-libjuju + pytest-operator used one connection with `model_context`
switching. With Jubilant, **one `jubilant.Juju` per model**:

```python
main = juju                       # the module 'juju' fixture (main cluster)
cos = jubilant.Juju(model="cos")  # explicit second model
cos.add_model("cos", cloud=k8s_cloud)   # or temp_model(...) for a random name
...
cos.destroy_model("cos", destroy_storage=True, force=True)
```

There is no implicit "current model"; pass the right `Juju` object explicitly.

### 8.9 Model owner (cross-model offers)

```python
# was: model_owner = untag("user-", cos_model.info.owner_tag)
owner = cos.show_model().owner       # already plain username, no "user-" prefix
# (fallback) owner = cos.cli("whoami", "--format=json", include_model=False)
```
Confirm `ModelInfo.owner` exists/spelled correctly in the installed version;
otherwise parse `show-model --format=json`.

### 8.10 `attach_resource` (test_k8s.py)

```python
# was: k8s.attach_resource("snap-installation", path, fileobj)
juju.cli("attach-resource", "k8s", f"snap-installation={path}")
juju.wait(jubilant.all_active)
```
No file object needed; the CLI takes `name=<path>`. Drop the `with open(...)`.

### 8.11 `terraform/setup.py`

This standalone script uses `juju.controller.Controller` to extract controller
auth (address, username, password, CA cert, cloud) into `TF_VAR_*`/`JUJU_*` env
vars. Jubilant does **not** expose controller auth objects. Replace with CLI:

- `controller.info()` / `controller.get_cloud()` →
  `juju.cli('show-controller', '--show-password', '--format=json', include_model=False)`
  and read `details.api-endpoints`, `details.cloud`, `account.user`, and the
  CA cert / password from the JSON (password requires `--show-password`).
- `controller.list_models()` → parse `juju models --format=json`.
- Make the whole script synchronous (drop `asyncio`).

This file is independent of the pytest suite; migrate it separately and verify
the `deploy-terraform` tox env still produces the same `TF_VAR_*`/`JUJU_*` env.

---

## 9. Gotchas checklist (verify each before merging)

### 9.1 CI / `operator-workflows` — HIGHEST RISK
`.github/workflows/integration_test.yaml` uses the reusable
`canonical/operator-workflows/.github/workflows/integration_test.yaml`, which is
built around **pytest-operator**. It bootstraps the controller and injects pytest
flags (model name, `--keep-models`, cloud/controller, charm path). Removing
pytest-operator removes the plugin that *defines* `--model`, `--keep-models`,
`--cloud`, `--controller`, so collection will fail with "unrecognized arguments"
unless our `conftest.py` defines matching options (§6/§7).
**Action:** read the pinned operator-workflows `integration_test.yaml` to confirm
the exact pytest arguments it passes, then ensure every one is defined in
`pytest_addoption`. Also confirm how it provides the built charm (it should
already be via `--charm-file`, which the repo already defines). Consider whether
operator-workflows has a Jubilant mode; if not, the conftest-shim approach above
is the safe path.

### 9.2 `exec`/`run`/`cli` raise on failure
`juju.exec`, `juju.run`, and `juju.cli` raise (`TaskError`/`CLIError`) on
non-zero exit. Places that **tolerate** failure today must wrap in `try/except`:
- `storage.py` cleanup loop (`k8s kubectl delete ...`) — currently ignores rc.
- `test_registry.py` cleanup (`k8s kubectl delete ...`).
- `test_k8s.test_verbose_config`: `ps axf | grep kube` — `grep` exits non-zero
  when there's no match → would raise. Guard it or use `... | grep kube || true`.
Conversely, **delete** the now-redundant `assert result.results["return-code"] == 0`
checks where you *want* failures to raise.

### 9.3 `config()` returns plain values, not metadata
python-libjuju `app.get_config()` returns `{key: {"value":..., "default":...,
"source":...}}`. Jubilant `juju.config(app)` returns `{key: value}`. The
`preserve_charm_config` fixtures (`test_k8s.py`, `test_smoke.py`) read
`app_after[key]["default"]` and `["value"]` — that structure is gone.
**Rewrite** preserve logic to: snapshot `before = juju.config(app)`; on teardown,
`juju.config(app, {k: before[k]})` for changed keys and
`juju.config(app, reset=[new_keys])` for keys the test added. You can no longer
read a key's default from the config dict — use `reset` to return to default.

### 9.4 `open_ports` is a list of strings
`u.safe_data["ports"][0]["number"]` → `UnitStatus.open_ports` is
`["2379/tcp", ...]`. Parse: `int(open_ports[0].split("/")[0])`
(`test_etcd.py`, `test_charmed_etcd.py`).

### 9.5 `public_address` comes from the status snapshot
`await unit.get_public_address()` and `unit.public_address` →
`juju.status().apps[a].units[uname].public_address`. Re-fetch status after
operations that could change addresses (add/remove unit).

### 9.6 Loss of async concurrency
`asyncio.gather(...)` patterns become sequential:
- `test_verbose_config` runs `ps axf | grep kube` on all units in parallel.
- `preserve_charm_config` gathers `get_config()` / `set_config()`.
- `test_nodes_labelled` gathers `set_config` / `reset_config`.
For correctness a sequential loop is fine. If wall-clock time matters, use
`concurrent.futures.ThreadPoolExecutor` (Jubilant calls are blocking subprocess
calls and release the GIL, so threads parallelize them safely).

### 9.7 No crash-dump
`--crash-dump`/`--crash-dump-args` in `tox.ini` are pytest-operator features.
Replace with a failure-triggered teardown fixture that calls `juju.debug_log()`
and/or shells `juju-crashdump`, or adopt `pytest-jubilant`'s `--juju-dump-logs`.

### 9.8 `wait` semantics differ
No `idle_period`; default `timeout=180s`, `successes=3`, `delay=1.0`. Always set
`timeout=` explicitly for deploys/upgrades. Use `error=jubilant.any_error` to
fail fast, and **omit** it to emulate `raise_on_error=False`.
`test_upgrade.py`'s custom `JujuUnitError`/`retry` loop should be rebuilt around
`juju.status()` reads + `jubilant.WaitError`/`TimeoutError`.

### 9.9 `base` vs `series` everywhere
`m.deploy(..., series=...)` and bundle `series:` keys → `base: ubuntu@XX.YY`.
`metrics_agent` fixture builds a `URL("ch", ..., series=..., architecture=...)`;
replace with `juju.deploy(name, channel=..., base=..., constraints={"arch": arch})`.

### 9.10 `constraints` is a mapping, not a string
`m.deploy(..., constraints="arch=amd64")` → `constraints={"arch": "amd64"}`.
The `Bundle.add_constraints`/`drop_constraints` helpers build constraint
**strings** in YAML — that's fine for bundle files (the CLI parses them), but any
direct `deploy(constraints=...)` call must pass a dict.

### 9.11 `charm_name`
`app.charm_name` → `st.apps[name].charm_name`. Used in `test_ceph.py`
(`ready_csi_apps`) to find ceph-csi apps.

### 9.12 Helper signatures change
`get_leader` returns a unit-name string (not index); `ready_nodes`,
`wait_pod_phase`, `get_rsc`, `get_pod_logs` currently take a python-libjuju
`Unit` and call `unit.run(...)`. Refactor them to take `(juju, unit_name)` and
call `juju.exec(cmd, unit=unit_name)`; read `task.stdout` / `task.return_code`.
`is_leader_from_status()` (in `get_leader`) → `unit.leader` from status.

---

## 10. Recommended migration order

1. **conftest options + `juju` fixture** (§6, §7) — re-add CLI options, add the
   `juju`/temp-model fixture. Keep everything else compiling.
2. **helpers.py primitives**: `get_leader`, `ready_nodes`, `get_rsc`,
   `wait_pod_phase`, `get_pod_logs`, `get_kubeconfig`, `cloud_arch`, `cloud_type`
   (§8.2, §8.7, §9.12). De-async, swap to `juju.exec`/`status`.
3. **Bundle/Charm/Markings**: drop `juju.url`/`juju.utils`, fix build fallback
   (§8.3, §8.4).
4. **Simplest tests first**: `test_dualstack.py`, `test_ipv6_only.py`,
   `test_smoke.py` — validate the fixture + helpers end to end.
5. **Medium**: `test_k8s.py`, `test_etcd.py`, `test_registry.py`,
   `test_openstack.py`, `storage.py`, `test_ceph.py`.
6. **Hard**: `test_upgrade.py` (custom wait), COS multi-model in `conftest.py`
   + `test_cos.py`.
7. **`test_charmed_etcd.py`**: compiles only (still `pytest.skip` at import).
8. **`terraform/setup.py`** (§8.11) — independent.
9. **Cleanup**: remove `juju`, `pytest-operator`, `pytest-asyncio`, `async-lru`
   from deps; fix `tox.ini` crash-dump; `uv lock`; run `ruff`/`mypy`.

---

## 11. Open questions to resolve before/while migrating

These cannot be answered from the test code alone and must be confirmed against
your Juju version, the installed jubilant build, and the CI workflow:

1. **operator-workflows behavior** (§9.1): exact pytest args injected; whether a
   Jubilant mode exists; how the built charm is handed to the test job.
2. **jubilant `Secret`/`RevealedSecret`/`ModelInfo` field names** (`owner`,
   `uri`, `content`) for §8.6/§8.9 — verify on `jubilant==1.10`.
3. **`show-controller` / `show-cloud` JSON shape** on your Juju (3.x) for §8.7.
4. **`add-k8s` invocation** (client vs controller, region, storage) for the COS
   substrate (§8.5) — confirm parity with `ops_test.add_k8s(skip_storage=False)`.
5. **Crash-dump strategy** (§9.7) — keep `juju-crashdump`, or switch to
   `debug_log` capture.
6. Whether to keep the **deploy-once / `--no-deploy` model-reuse** pattern
   (`kubernetes_cluster.is_deployed`) or move to per-module temp models — this
   affects CI time and the COS/cross-model fixtures.

---

## 12. Sources

- Jubilant repo: https://github.com/canonical/jubilant
- Jubilant API reference: https://documentation.ubuntu.com/jubilant/reference/jubilant/
- How to migrate from pytest-operator to Jubilant (Jubilant docs):
  https://documentation.ubuntu.com/jubilant/how-to/migrate-from-pytest-operator/
- How to migrate integration tests from pytest-operator (Ops docs):
  https://documentation.ubuntu.com/ops/latest/howto/migrate/migrate-integration-tests-from-pytest-operator/
- Real-world migration PR (reference patterns):
  https://github.com/canonical/discourse-k8s-operator/pull/326
- python-libjuju: https://github.com/juju/python-libjuju
- pytest-jubilant: https://github.com/canonical/pytest-jubilant
</content>
</invoke>
