# Service Contracts

`cli_wrapper.py` defines `ServiceCLI` and `Service`; `models.py` holds Swarm inspect snapshots.
Shared Swarm association, cleanup and launch deadlines stay in [the components guide](../AGENTS.md).
Service `create` uses the [runtime environment transport](../../AGENTS.md#container-environment-transport).

## Local Navigation

- `_update_options.py` validates and encodes rollout policy options before command construction.
- `ServiceCLI.update()` builds the CLI; `Service.update()` forwards the same policy and timeout options.
- `ServiceCLI.create()` returns an immutable-ID `Service` and has no rollout-policy parameters.
- `ServiceCLI.list(hydrate=True)` resolves the truncated listed IDs, then pins snapshots by full ID; the default keeps its listing plus one resolve launch. `update`, `scale`, `rollback` and `remove` release supplied handles' pins even when the launch fails.
- Read-only generation capture and readiness evaluation belong to [convergence](../../convergence/AGENTS.md), not this wrapper.

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
- Update validates timeout, then both policy sets, before executable lookup. The handle must preserve
  this validation path, every option, and older positional argument bindings.
- Update and rollback return `None`, issue their selected mutation once, and add no probe or retry.
  CLI completion is not application-health evidence; it must not silently become a convergence wait.

## Log Streams

- `ServiceCLI.logs(stream=True)` returns owned `ProcessStream` records `(source, bytes)`;
  collected logs stay `str`. Use its context manager or close early under the runtime ownership rules.
- There is no `Service.logs()` handle API. Keep inspection and timeout forwarding as the parent documents.

## Regression References

Paths are relative to `tests/pywhales/`; read [test-selection hazards](../../../tests/AGENTS.md) before running mixed modules.
- `components/test_service_rollout.py`: fake local Python CLI, positional compatibility, handle forwarding,
  zero values, bounds, validation before lookup, and `test_rollback_single_launch`; no daemon required.
- `test_service_rollout_types.py`: static `ty` contracts, not pytest execution.
- `components/test_service.py`: mocked inspect/cleanup/log cases and fake-CLI hydrated listings (`hydration_fakes.py`) mixed with real Swarm operations.
- `components/test_deadline_forwarding.py`: service budget forwarding, scale preflight and validation order.
- `components/test_service_rollout_live.py` needs `PYWHALES_LIVE_SERVICE_ROLLOUT=1` and an explicitly
  selected disposable manager; it creates/removes a service. Never use it as a workstation smoke test.
