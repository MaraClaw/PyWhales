# Test Guide

Paths are relative to `tests/`. Repository-wide checks and safety rules are in the [root guide](../AGENTS.md).

## Choose tests by behavior

| Area | Start here |
| --- | --- |
| Client configuration, process ownership, buffering, timeouts | `pywhales/test_client_config.py`, `pywhales/test_docker_client.py`, `pywhales/test_utils.py` |
| Hydrated listings, pinned snapshots and monotonic cache expiry | `pywhales/components/test_{container,image,network,volume}.py`, `pywhales/test_client_config.py` |
| Image fanout under shared child limits | `pywhales/components/test_image_child_limits.py`, `pywhales/test_observation.py`; real-child lifecycle in `pywhales/components/test_image.py` |
| Child environment selection and launch snapshots | `pywhales/test_environment.py`, `pywhales/test_inventory_cli.py`, `pywhales/components/test_env_forwarding.py`, `pywhales/components/test_env_branch_transport.py` |
| Bulk reload and inventory (13 kinds, including manifest lists) | `pywhales/test_bulk_reload.py`, `pywhales/test_bulk_reload_bounded.py`, `pywhales/test_inventory.py`, `pywhales/test_inventory_cli.py` |
| Observation, launch limits, bounded capture | `pywhales/test_observation.py`, `pywhales/test_observation_budget_integration.py`, `pywhales/test_launch_limits.py`, `pywhales/test_bounded_capture.py` |
| Direct-archive stderr allowances and concurrent output draining | `pywhales/test_inventory_types.py`, `pywhales/test_observation.py`, `pywhales/test_bounded_capture.py`, `pywhales/test_telemetry_archive_lifecycle.py`, diagnostic cases in `pywhales/components/test_image.py` |
| Safe/raw diagnostics, validation, command-copy isolation | `pywhales/test_inspect_diagnostics.py`, `pywhales/test_logging.py`, `pywhales/test_exceptions.py`, `pywhales/test_automatic_diagnostics.py` |
| Telemetry records, exporter, streams, archives | `pywhales/test_telemetry*.py` |
| Closed stop reasons and exporter offer counts | `pywhales/test_telemetry_stop_reasons.py`, `pywhales/test_telemetry_stop_runtime.py` |
| Admission decision telemetry and launch correlation | `pywhales/test_admission_telemetry.py`, `pywhales/test_admission_telemetry_correlation.py`, `pywhales/test_admission_telemetry_locks.py` |
| Active declared demand across admission classes | `pywhales/test_admission_owner_demand.py`, `pywhales/test_admission_owner_demand_streams.py` |
| Convergence, Compose completion, reconciliation journal/store | `pywhales/test_convergence.py`, `pywhales/test_convergence_types.py`, `pywhales/test_reconciliation.py`, `pywhales/test_reconciliation_store.py` |
| Other opt-in policy packages | `pywhales/test_admission_*.py`, `pywhales/test_profiles*.py`, `pywhales/test_storage_*.py` |
| Delivery chain; offline fleet and script tooling | `pywhales/test_delivery_workflows.py`; `pywhales/test_fleet_*.py`, `pywhales/test_transport_experiment.py`, `pywhales/test_plugin_installer.py`, `pywhales/test_docker_overhead_benchmark.py` |
| Command groups, transport, deadlines, wire models and enum/JSON regressions | [Component selection guide](pywhales/components/AGENTS.md) |

Read module-level code and fixtures before calling a selection unit-only. Fake CLIs and real Python/shell children don't imply daemon use or process-free tests.
`pywhales/test_delivery_workflows.py` runs a loopback HTTP server, `tar`, `python -m scripts.delivery`, and an offline `uv` install
smoke (`UV_OFFLINE=1`), so don't call that module install-free; fleet, installer, and benchmark tests need no Docker.

## Runtime regression contracts

Filenames in this section are relative to `pywhales/`.
- Deadline modules cover run/exec/Compose/build/bake, logs/events, pull/push/copy, and push/start/watch/prune. Keep keyword-only overloads,
  reject invalid timeouts before launch, keep inspect/pull preflights outside the budget, and cover partial output, once-only errors, early close/draining.
- Use real children to prove reaping, closed pipes, stopped reader/watchdog threads, early close before consumption, and saturated-queue backpressure.
  Mocks don't prove cleanup or launch-time snapshots. `test_logging.py` keeps concurrent-client command/list composition and `copy()`/`deepcopy()` isolation.
- Empty streaming image pull is a repeatedly closeable/context-managed `EmptyImageStream` without executable resolution:
  runtime cases are in `components/test_image.py`; static returns in `test_type_contracts.py` and `test_command_deadline_types.py`.
- Hydrated listing cases in the four resource modules use `components/hydration_fakes.py` without real children. Keep exact-ID/name matching, bounded launch counts, affected-chunk disappearance re-probes, image CLI ordering, pin release and fresh container cleanup. `test_client_config.py` covers monotonic/negative-age expiry, current validity periods, copy/pickle resets, failed reload pin preservation and unpinned bulk publication.
- `components/test_image_child_limits.py` uses barrier/event fake children with real shared reservations, without processes or sleeps. Keep nested occupied allowances, creator-context sizing at first iteration, four-worker unbounded behavior, zero-slot refusal without debit and immediate sibling-race refusal; mocks don't replace the real-child cleanup cases in `components/test_image.py`.
- Assert raw failure details through `stderr`, `stdout`, or `docker_command`, not safe exception text. Test safe and raw rendering
  separately, including traceback chains and no inspect dumps in safe mode; raw opt-in must not change returned payloads.
- Strict workload classification local-child cases live in `test_utils.py`, `test_exceptions.py`, and `test_client_config.py`: preserve captured and early/exhaustion stream errors, per-launch snapshots, and default, detached, and preflight controls.
- `test_inspect_diagnostics.py` rejects empty/multiple singleton results, non-object/non-array shapes, and invalid JSON.
  Identity-changing `reload()` must preserve the cache object and refresh timestamp; matching full IDs pass.
  The current-context handle (`Context(..., None)`) is exempt from name stability and accepts a changed current name.
- Bulk reload covers 13 kinds. Manifest lists use singleton inspect per reference, handle-specific `insecure`, and strict safe validation;
  public manifest inspect stays lenient. Builder uses one listing per config, names never in argv, identical lines collapse, malformed/conflicting lines reject.
  Preserve exact config-identity grouping, upfront rejection, unchanged caches/timestamps on later failure, and one publish per handle.
  Inventory returns ordered staged records without cache publication, detached from handles both ways, including hydrated manifest flags.
- `test_environment.py` keeps `inherited_environment_names=None` on legacy inheritance, including the restricted platform base when `inherit_environment=False`. A supplied collection overrides either mode; empty inherits nothing. Keep copied `frozenset` selection, bare-string/invalid-name rejection without disclosure, supplied-config precedence, missing-name omission and Windows case equivalence.
  Freeze selected values before argv conversion, consume matching frozen scopes once, and preserve snapshots through image load/save and inventory chunks (`test_inventory_cli.py`). Keep client then operation overlay precedence, mandatory Buildx values last, unchanged process-PATH lookup, safe diagnostics and limits. `components/test_env_forwarding.py` and `test_env_branch_transport.py` cover selected/excluded ambient collisions, explicit-overlay collisions, strict rejection and bound assignments surviving invalid config mutation; `test_profiles_validation.py` covers endpoint invalidation.
- Completion tests keep exited-zero success, safe nonzero failure, unfinished counters, and final-pass revalidation. Preserve closed
  `completion_failed` journal reasons, generation-fenced deadline-detail round-trips in the store, and static completion types.
- Convergence batching cases in `test_convergence.py` compare reason/cause with single-ID walks, count policy-sized chunks, and cover response-limit fallback, launch checkpoints, complete confirming passes and hung-batch cleanup. Delivery retry cases belong in `test_delivery_workflows.py`; transport and command-deadline contracts live in `scripts/delivery/AGENTS.md`.
- `test_convergence.py` covers opt-in `fail_on_exit` for Compose and standalone containers on the first and confirming passes, exit zero and nonzero, unchanged default behavior, and completion/restart budgets. `test_reconciliation.py` covers cause-free `exited` projection to the journal without exit-code/output payloads.
- The same modules cover opt-in `fail_on_unhealthy` on first/confirming passes, eligibility and failure precedence,
  and the closed `unhealthy` journal reason with cleared deadline detail and no mutation replay. Static flags and
  failure reasons live in `test_convergence_types.py`.
- Standalone `capture_containers()` cases in `test_convergence.py` resolve names/prefixes to full IDs and reject duplicate resolved IDs.
  Disappearance/recreation is `superseded`; recheck readiness/completion on the final pass, retain the latest complete deadline cycle, and reap hung inspections on cancellation.
  Keep `ContainerTarget` and capture/wait return contracts in `test_convergence_types.py` aligned.
- Node readiness uses the same fake-CLI module: fixed IDs, explicit manager subset, recovery despite version changes, demotion/missing manager status, malformed data, disappearance, confirming-pass regression, partial-cycle retention and hung-inspect cancellation. Keep node blocker counts bounded and integer-only through `test_reconciliation_store.py`; static node contracts live in `test_convergence_types.py`.
- Observation tests keep nested minimum limits, shared response accounts across threads, and deadline and cancellation precedence.
- `test_inventory_types.py` covers `max_total_io_bytes` defaults and positive exact-int validation. `test_observation.py`, `test_observation_budget_integration.py` and `test_bounded_capture.py` cover atomic nested/copied accounts across output reads and accepted stdin writes, one detection byte, exact EOF, source-limit precedence, retained scopes without timers, and excluded direct stdin/archive drains/post-exit stderr. Captured input still needs a separate stdin budget; filename payloads stay outside accounting.
- `max_total_archive_stderr_bytes` has separate positive exact-int validation in `test_inventory_types.py`; `test_observation.py` checks nested/copied accounts, one shared detection byte, exact EOF and no later read after overflow. `test_bounded_capture.py`, `test_telemetry_archive_lifecycle.py` and diagnostic cases in `components/test_image.py` prove actual post-reap stderr accounting, noisy load stdout/stderr draining during upload, paused-save diagnostics, scope retention, raw failure/classification precedence, and reaped children/closed pipes/joined workers. Preserve unset legacy behavior and separate stream/aggregate I/O exclusions; `test_telemetry_stop_reasons.py` maps the new exception reason to existing `other_limit`.
- Concurrent-child validation and atomic nested attempt/child reservations live in `test_observation.py`;
  `test_bounded_capture.py` proves shared-slot contention, reuse at reap independently of drain completion, failed setup and terminal
  cleanup across four execution families, plus sequential inventory with one child slot.
- Launch-reservation tests cover exact-cap copied contexts, atomic all-account debit, non-consuming inventory plan checks, staged-cache preservation on races, failed-spawn charging, four execution families and lazy-save inspect accounting. Keep local-child cleanup evidence in the focused observation/inventory/bounded-capture selections.
  Telemetry tests keep records payload-free under raw diagnostics, one completion per launch, and results intact when the observer raises.
- `test_telemetry_refusals.py` covers schema-11 preflight refusals across captured, stream, archive load and lazy save, including child saturation without attempt consumption: exactly one refusal, no launch/completion/spawn/input read, original exception, admission correlation, observer snapshots and disabled ID/clock fast paths. Earlier validation stays silent; post-start native argv errors keep launch plus limit completion. Archive lifecycle and admission telemetry tests cover adjacent paths.
- `test_telemetry_stop_reasons.py` covers closed reason projection (including `total_io` and unknown-to-`other_limit`), keyword-only field compatibility, immutable complete counts, repeated/concurrent offers, overflow and shutdown without recount. `test_telemetry_stop_runtime.py` checks command/scope deadlines across all four families and reasons retained after stream terminal cleanup; existing exporter/observer tests keep unlocked sinks and the disabled observer fast path.
- Admission telemetry tests cover decision outcomes, callbacks outside locks, cross-thread cancellation, and no new IDs or clock reads without an observer. Correlation tests cover nested calls and stream lifecycle launches.
- `test_admission_pending_demand.py` covers class queue-demand bounds; `test_admission_owner_pending_demand.py`
  covers owner-wide bounds across classes, immediate-grant bypass, precedence, contention, reuse and cleanup.
  Keep `pending_demand_full`/`owner_pending_demand_full`, saturating counters and schema-11 outcomes aligned without
  changing prior positional snapshot fields; static contracts live in `test_admission_types.py`.
- `test_admission_owner_demand.py` and `test_admission_owner_demand_streams.py` cover active demand across classes: exact fast/queued grants, impossible-demand validation, resize growth yielding to any class's queue when enabled, shrink/release wakeups, class FIFO and queue-limit precedence, retained stream demand, saturating `rejected_owner_demand_full` and unlocked schema-11 decisions.
- `test_profiles.py` and `test_profiles_validation.py` cover startup health-interval approval, exact ceilings,
  conditional runtime control, omission and tampered-plan rejection before preflight.
- Leadership cases in `test_convergence.py` cover omitted-false versus explicit malformed values, enabled designated managers only, zero/one/multiple leaders, changing leaders across clear passes and partial-cycle detail. Keep `leader_mismatch_managers` bounded in type and store contracts.
- Offline `test_fleet_harness.py` keeps FIFO head blocking under per-action caps, global/per-action peaks, and queue expiry without counting unlaunched actions in peaks.
  Leased retry cases in `test_fleet_harness.py`, `test_fleet_harness_cli.py`, and `test_fleet_report.py` cover attempt lineage, FIFO, the original deadline, one logical outcome and queue sample, and legacy digest compatibility. Preserve old sequential report imports and reject contradictory schedules; CLI tests check safe export/decide import rejection without candidate launches.
- `test_fleet_backoff.py` covers fixed and capped exponential backoff outside capacity, deadline/tie ordering, stop/drain, overflow/subnormal arithmetic, mixed policies, rejected-plan versions, independently authored histories and tampered imports. Imports must reject an unlaunched terminal after another cancellation establishes early stop, while permitting active drain and same-time expiry. Legacy schedules stay implicit v1, fixed-only plans use local v2, and any exponential policy selects v3, including rejected plans; binding stays v1 and legacy operation digests stay unchanged.
- `test_fleet_binding.py` covers canonical manifest/sanitized-environment fingerprints, same-revision policy rebinding refusal, tampered environments, legacy unbound reports and baseline environment mismatches. Keep run/export/decide offline, permanently unqualified and `no_go`, with CLI routing and zero candidate connections.

## Runtime fixtures and shared state

- `conftest.py` supplies session-scoped `docker_client`; function-scoped `ctr_client` returns that same client, not an isolated
  daemon. It probes `<executable> version` for 10 seconds; a missing executable, failed probe, or timeout skips, or fails with
  `--no-runtime-skip`. `--docker-exe` configures only this fixture, not global or separately built clients or plugin availability.
- Registry fixtures can pull `mhenry07/apache2-utils` and `registry:2`, bind host port 5000, and yield `localhost:5000`. Colocate the
  runner with the disposable daemon so `localhost` registries, published ports, and bind mounts resolve; a remote `DOCKER_HOST` alone
  doesn't. `docker_registry` logs in and out with test credentials, changing local login state; `docker_registry_without_login` doesn't.
  Don't run host CI provisioning scripts to fix workstation access. `swarm_mode` initializes Swarm with fixed advertise address
  `172.31.155.129` and force-leaves during teardown.
- Integration tests include daemon-wide pruning in `pywhales/components/test_system.py` and builder-cache pruning in the Buildx suite.
  Pydantic 2 deprecation warnings are errors; component-specific collection and shared-state hazards are in the linked selection guide.
- Documentation `pywhales/conftest.py::daemon` mocks `docs.docs_utils.docker` (named resources, context cleanup, inventory/name helpers)
  and makes `subprocess.run` raise `AssertionError`. `pywhales/test_docs_live.py` exercises that mock, not a live daemon.

## JSON fixture rules

`get_all_jsons(<category>)` in repository-root `pywhales/test_utils.py`, not `tests/pywhales/test_utils.py`, sorts every entry in
`tests/pywhales/components/jsons/<category>/` by `int(path.stem)`, filtering nothing. Only numbered JSON files: no `AGENTS.md`,
READMEs, nonnumeric files, or subdirectories. Preserve fixed names (`test_parsing_events` reads `events/0.json`) and snapshot meaning.

## Static contracts

Modules named `*_types.py` or `*_type_contracts.py` under `pywhales/` hold `_check_*` functions with `assert_type`: pytest neither collects
nor runs them, and ty validates the inferred types (deadline shapes: `test_command_deadline_types.py`). `test_inventory_types.py` and
`components/test_task.py` mix one with runtime tests. Update contracts with overloads, return types, and top-level aliases.
