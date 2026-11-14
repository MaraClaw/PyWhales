# Service Contracts

`cli_wrapper.py` defines `ServiceCLI` and `Service`; `models.py` holds Swarm inspect snapshots.
Shared Swarm association, cleanup and launch deadlines stay in [the components guide](../AGENTS.md).
Service `create` (`envs`) and `update` (`env_add`) use the [runtime environment transport](../../AGENTS.md#container-environment-transport).

## Local Navigation

- `_spec_options.py` holds one ordered table of the options `create` and `update` share and encodes both forms; `_update_options.py` validates and encodes rollout policy options. Both run before command construction.
- `_logs.py` assembles the `service logs` command for `ServiceCLI.logs()` and `TaskCLI.logs()`.
- `ServiceCLI.update()` builds the CLI; `Service.update()` forwards every option, including the policy and timeout options.
- `ServiceCLI.create()` returns an immutable-ID `Service` and takes the same `update_*` and `rollback_*` policy options as `update()`.
- `ServiceCLI.list(hydrate=True)` resolves the truncated listed IDs, then pins snapshots by full ID; the default keeps its listing plus one resolve launch. `update`, `scale`, `rollback` and `remove` release supplied handles' pins even when the launch fails.
- Read-only generation capture and readiness evaluation belong to [convergence](../../convergence/AGENTS.md), not this wrapper.

## Spec Options

- Create and update emit the same options from one table. Create writes `--option`; update writes `--option-add` and `--option-rm` for list options and the plain flag for the rest. Plain-flag list options (`cap_add`, `cap_drop`, `log_opts`) and scalars keep one name in both forms.
- Every option after the old positional parameters is keyword-only, and `timeout` stays last. Never reorder or insert positionals; `test_service_cli29.py` pins the legacy positional lists.
- Create-form table order is the legacy argv order, with new options slotted between: edit it only together with the byte-for-byte legacy argv tests. Update emits force, image, registry auth, detach and quiet first, then the table, then policies.
- Table slot names are the underscore flag stems (`env`, `constraint`, `no_healthcheck`); `create()` maps its older plural parameter names (`constraints`, `labels`, `healthcheck`) onto them, and a test checks that every slot has a parameter. Adding a table entry means adding the parameter, its docstring entry, the handle forward and an argv test.
- Only `None` omits a value; zero, `False` and empty strings are sent. `init`, `tty` and `read_only` are tri-state on update (`--x`, `--x=false`, omitted) and plain flags on create.
- Durations: `stop_grace_period`, `restart_delay` and `restart_window` encode through `_update_options._duration` (exact nanoseconds, bounds checked, like the rollout policies). The `health_*` options keep whole-second `to_seconds`. Neither is a local deadline.
- Ports accept the container `publish` tuples or long-form mappings. Mounts, secrets and configs are long-form mappings joined with commas. `args` is a sequence shell-quoted into one `--args` value, so `[]` clears the arguments. `host_add`/`host_rm` are `host: ip` mappings.
- `envs` and `env_add` use the [runtime environment transport](../../AGENTS.md#container-environment-transport): eligible values reach the child through per-launch overrides, never argv. Encoding happens before executable lookup, so strict transport rejects without a launch.
- Validation order for create and update is `timeout`, then rollout policies, then spec options, all before executable lookup. Wrong shapes raise `TypeError`; an unsupported `mode`, a bad port tuple or an out-of-range duration raises `ValueError`. Messages stay value-free.
- `--rollback` and its restrictions on other flags are enforced by the CLI, not locally. Update still adds no probe, retry or convergence wait.

## Update And Rollback Policies

- `update()` exposes separate `update_*` and `rollback_*` policy sets: parallelism, delay,
  failure action, monitor, maximum failure ratio, and order. Never merge their flag namespaces.
- These configure policy on `service update`; `ServiceCLI.rollback()` is the distinct rollback action.
- Only update failure action accepts `rollback`; both policy sets accept `pause` and `continue`.
- Orders are `stop-first` or `start-first`. Explicit zero parallelism, delay, monitor and ratio survive;
  only `None` omits an option. Do not route numeric values through truthy flag helpers.
- Parallelism is unsigned 64-bit; duration values are nonnegative signed-64-bit nanoseconds.
  Integer seconds and `timedelta` encode as nanoseconds, not rounded seconds or local CLI timeouts.
- Failure ratios are finite values in `[0, 1]`; reject booleans and invalid policy values locally.
- Create and update validate timeout, then both policy sets, then spec options, before executable lookup.
  The handle must preserve this validation path, every option, and older positional argument bindings.
- Update and rollback return `None`, issue their selected mutation once, and add no probe or retry.
  CLI completion is not application-health evidence; it must not silently become a convergence wait.

## Log Streams

- `ServiceCLI.logs(stream=True)` returns owned `ProcessStream` records `(source, bytes)`;
  collected logs stay `str`. Use its context manager or close early under the runtime ownership rules.
- `TaskCLI.logs()` has the same overloads and ownership but no service inspection: it launches
  `docker service logs <task>` once, because Docker accepts a task ID there. Validation of `timeout` still comes first.
- There is no `Service.logs()` handle API. Keep inspection and timeout forwarding as the parent documents.

## Inspect And Listings

- `inspect(pretty=True)` returns the CLI text for the whole request as `str`: no parsing, no snapshot cache or pin, and `[]` returns `""` without a launch. The default overloads keep their handle returns.
- `ps(x, filters=...)` takes ordered filter pairs and rejects a mapping with `TypeError` before the empty-selection return; `Service.ps(filters=...)` forwards them.

## Regression References

Paths are relative to `tests/pywhales/`; read [test-selection hazards](../../../tests/AGENTS.md) before running mixed modules.
- `components/test_service_rollout.py`: fake local Python CLI, positional compatibility, handle forwarding,
  zero values, bounds, validation before lookup, and `test_rollback_single_launch`; no daemon required.
- `components/test_service_cli29.py`: recording fake CLI (`argv_fakes.py`); legacy create argv, one argv case per
  create and update option, handle forwarding, validation order and before lookup, `inspect(pretty=True)` and `ps` filters.
- `components/test_task_logs.py`: task logs argv, ownership, validation and deadline against the fake CLI.
- `test_service_cli29_types.py` and `test_task_logs_types.py`: static `ty` contracts, not pytest execution.
- `test_service_rollout_types.py`: static `ty` contracts, not pytest execution.
- `components/test_service.py`: mocked inspect/cleanup/log cases and fake-CLI hydrated listings (`hydration_fakes.py`) mixed with real Swarm operations.
- `components/test_deadline_forwarding.py`: service budget forwarding, scale preflight and validation order.
- `components/test_service_rollout_live.py` needs `PYWHALES_LIVE_SERVICE_ROLLOUT=1` and an explicitly
  selected disposable manager; it creates/removes a service. Never use it as a workstation smoke test.
