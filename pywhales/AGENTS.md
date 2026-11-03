# Shared Runtime Internals

## Configuration And Policy

- Resources retain the originating `ClientConfig`; equality is immutable ID plus config equality. `add_simple_arg()` omits only `None` (keeps `0`, `False`); `add_flag()` uses truthiness; optional Docker globals use `is True`. Compose globals sit between `compose` and the subcommand (`get_compose_command()`); explicit per-call `progress` wins and `compose_env_files` order matters.
- Lookup is lazy: `get_client_call_with_path()` caches `shutil.which()` plus prefix arguments, not globals rebuilt by `docker_cmd`. Its leading `PolicyExecutable` carries config through `Command`/list composition, `copy()` and `deepcopy()`; get execution policy before stringifying argv.
- Each launch snapshots buffer budget, diagnostics, `strict_workload_errors`, observer by reference, environment, and default or innermost `execution_timeout()` for the identical config; later mutations affect new launches only. `DockerClient(client_config=...)` retains the supplied config unchanged; other configuration arguments don't override it.
- `inherit_environment=True` is the `DockerClient`/`ClientConfig` default. False retains only ambient `PATH`/`HOME`, plus `USERPROFILE`/`SYSTEMROOT` on Windows, then applies client and launch overlays; Windows names merge case-insensitively.
  `get_execution_policy()` centrally freezes that restricted full environment before argv stringification. Executable lookup still uses process `PATH`; child settings never modify `os.environ`.
- `ReloadableObject` stores immutable identity; `is_immutable_id=True` skips initial inspect, `CACHE_VALIDITY_PERIOD = 0.01` seconds expires snapshots, and `reload()` forces inspection. Reload requires exact stored-identity equality unless the stored identity is `None`; mismatch raises `ParsingError` without publishing cache or timestamp.
- `ReloadableObjectFromJson._parse_single_inspect_json()` requires a singleton array containing an object, not a bare object, empty/multiple results, or a scalar; reject shape errors with `ParsingError` before model parsing.
- `DockerCamelModel` accepts names and aliases; preserve `to_docker_camel()` exceptions (`ExecIDs`, `IPAddress`, `OOMKilled`, `RootFS`, `DefaultAPIVersion`).
- `exceptions.py` uses ordered line-start stderr patterns, not substrings, names, or context paths. By default, forwarded application stderr can still select a specialized exception. `DockerClient(strict_workload_errors=True)` opts explicitly marked workload-status launches into exact `DockerException` for nonzero exits; raw attributes and diagnostic rendering stay intact.
- Wrappers pass `workload_status=True` for attached container `run`/`execute`/`start` and non-detached Compose `run`/`execute`. Detached calls, image/inspect preflights, and other commands retain classification. Never infer the mark from argv or exit code. Keep captured `run`, `ProcessStream._notify_exit_failure`, and stream exhaustion aligned; timeout, cancellation, limits, and telemetry outcomes are unaffected.

## Container Environment Transport

- `_environment.py::container_environment` is shared by container `create`/`run`/`execute`, Compose `run`/`execute`, service `create`, and Buildx/Compose `build` args.
- Safe noncolliding names become `--env KEY`; values move to per-launch overrides bound to the effective child-environment snapshot, never to process-global `os.environ`.
- Reserved CLI/runtime names or prefixes, invalid names/values, non-string keys, and effective-child environment collisions retain ordered literal `KEY=value` arguments rather than new rejection or env-file behavior. Filtered-out ambient names don't collide.
- A matching frozen scope supplies collision checks without consuming its snapshot; only the next policy snapshot for the identical config consumes it. Windows checks names case-insensitively; all equivalent mapping entries stay literal.
- Existing stringification (including non-string values) and empty values survive. `_ContainerEnvironment` binds the collision snapshot so later ambient/client changes cannot change the launch decision.
- This reduces argv exposure, not universal redaction: literal fallbacks, child environments, returned data, raw exception attributes, and caller logging can still carry values; see confidentiality below.
- Compose receives the same child overrides for config interpolation; these are not a container-only secret channel. Tests: `tests/pywhales/components/test_env_forwarding.py` and `test_env_branch_transport.py`.
- `strict_env_transport=True` on `DockerClient`/`ClientConfig` rejects any literal assignment fallback with a value-free `ValueError`; build args reject before lookup/inspect. The default is false.
  Reserved prefixes include `PYTHON`, so `PYTHON_VERSION` rejects in strict mode. Bake `--set` is unaffected. Image history, provenance, child environments and Compose interpolation remain possible value exposure surfaces.

## Bulk Reload And Inventory

- Both support 13 kinds: Container, Image, Network, Volume, Node, Service, Config, Secret, Task, Plugin, Context, Builder, and ManifestList (`manifest_list` in `InventoryKind`).
- Bulk reload rejects unsupported kinds and missing identities before CLI lookup; empty input is inert. Group by exact config object identity plus kind, not config equality; inspect each distinct handle once.
- Builders use one strict `buildx ls --format "{{json . }}"` per group (`parse_builder_listing`): exact names never reach argv, with ordered handles per request. Only byte-identical lines collapse; non-builder data, null/empty names, and conflicting entries reject. Safe mode withholds names.
- Manifest lists require per-reference inspection, never generic batching; each handle's `insecure` flag follows its launch. Strict helper and lenient public inspection differ: [manifest guide](components/manifest/AGENTS.md).
- Stage all groups before any cache/timestamp publication; staged identity equals stored identity exactly without freshness accessors. Any group failure leaves all originals unchanged; each distinct handle publishes once.
- This is neither a daemon transaction nor atomic for concurrent readers: groups inspect separately and successful handles publish sequentially.
- `capture_inventory()` never publishes: ordered detached deep-copied records retain duplicates and leave inputs untouched.
- Check reference totals before launching; chunks obey `max_references` and launch limits, with each group's measured environment frozen. Alias drift, inconsistent snapshots, or cross-chunk Swarm ambiguity reject capture.
- `_inventory_staging._stage` checks the complete chunk plan against every enclosing launch account's remaining allowance before its first inspect: ordinary chunks, one builder listing per group, singleton manifests. This consumes no reservation; concurrent consumption can still refuse a later launch before any original cache is published.
- Bulk reload uses bounded staging only with an explicit `policy` or active `observation()`; tests: `tests/pywhales/test_bulk_reload*.py` and `test_inventory*.py`.

## Observation And Subprocesses

- `observation(policy, deadline=, cancel=)` intersects per-command limits, non-`None` totals and finite monotonic deadlines; all enclosing tokens remain effective. Entry/exit check cancellation before deadline.
- Captured bytes charge every enclosing locked `_ResponseAccount` before reporting overflow; streams charge separate `_StreamAccount`s (`ObservationState.stream_accounts`, default `()`), never each other's accounts.
- `InspectionPolicy.max_total_launch_attempts` and `max_concurrent_children` are `None` or positive exact integers, never bool. Each explicitly bounded scope adds shared accounts; nested and copied contexts retain the outer accounts. `check_scoped_launch` reserves once after cancellation/deadline and argument preflight: lock attempts before children, outermost first within each family, and check all before debiting any. Attempt refusal is `ObservationLimitError("total_launch_attempts")`; interruption, spawn failure, or CLI failure never refunds an accepted attempt. Captured calls, streams, archive loads and lazy saves share this seam; separate inspect preflights consume their own attempts.
- Child saturation raises `ObservationLimitError("concurrent_children")` immediately, without waiting, spawning or consuming an attempt. `_ChildReservation.release()` returns child slots exactly once at local reap, independently of output-drain completion or stream exhaustion, or after failed setup leaves no owned child. It bounds local CLI children only, not daemon workloads or admission permits. `test_observation.py`, `test_bounded_capture.py`, and `test_telemetry_refusals.py` under `tests/pywhales/` cover atomic reservations, lifetimes and refusals.
- `binds_launches` (deadline, token, stream, stdin or child account) retains stream/archive scope snapshots; a budget alone adds no wait timer.
- `run()` checks observation before timeout/input branches: observed calls always use `bounded_capture` and reject TTY, uncaptured output, or input without `max_total_stdin_bytes` with `ValueError`. Budgeted bytes or cooperative byte iterables upload while stdout and stderr drain.
- Stdin writes lock all enclosing `_StdinAccount`s outermost first across one nonblocking write and charge only accepted bytes, separately from response and stream budgets. Exact-budget EOF succeeds; further nonempty input raises `total_stdin`. Archive uploads retain the same accounts after scope exit. Caller iterator code cannot be interrupted; cleanup joins the owned writer after it yields.
- Only unobserved calls use `subprocess.run()` (no timeout/input) or `Popen.communicate()` (timeout/input, with owned stdin-writer cleanup).
- `check_scoped_launch()` precedes `Popen` and launch telemetry; its observation refusals emit a refusal record when observed. `E2BIG` becomes `ObservationLimitError("native_argv")` after launch telemetry starts.
- Capture precedence is cancellation, then a command deadline strictly earlier than the scope's while the child runs, then scope expiry. Tokens add only `CANCEL_CHECK` (50 ms) waits.
- Streams/archives snapshot once at launch (lazy `save` at first iteration); watchdog or `ArchiveDeadline` keeps enforcing it after scope exit.
- `ProcessStream` charges raw stdout/stderr reads under the queue lock, limiting reads to smallest `stream_allowance` + 1. Overflow records `ObservationLimitError("total_stream")`, kills/reaps, and delivers once; readers never raise it.
- `ArchiveDeadline` charges only caller-thread `read()` and expires via `_Stop("limit")`; watchdog drains and post-exit stderr `record_read` do not charge, preserving `NoSuchImage` precedence.
- Timeouts are `None` or positive finite seconds, never bool, validated before launch. Monotonic launch deadlines advance while paused; expiry kills/reaps then raises `DockerTimeout` with output. Natural nonzero exits follow the workload-classification policy above and keep post-exit draining.
- Bare `break` does not close an owned stream: use a context manager or `close()`, even before iteration. Closing cancels only the local CLI and waits for cleanup; it is idempotent.
- Recorded errors remain deliverable once after close or at clean context exit; cleanup must not replace an active body exception.
- POSIX workers wait on pipe readiness plus `WakeLatch`; a reaper publishes exit. Only deadlines, tokens, or Windows pipe slices time waits.
- Every stop path sets the latch; close it after joining waiters. `ArchiveDeadline` holds its pipe lock while waiting, so expiry cannot close a pipe beneath a wait.

## Buffers, Errors, And Confidentiality

- `stream_buffer_bytes=None` is unbounded; budgets are positive integers, not bool. Omitted `DEFAULT_BUFFER` inherits at the shared boundary; explicit `None` stays unbounded. The separate non-`None` image override wins without config normalization; see the image guide.
- Stdout/stderr queued payload shares one budget; unfinished pipe records and stderr history have separate bounds, not a total-memory cap.
- Oversized records raise `BufferError`; pressure blocks rather than drops. Close/failure wakes waiters; terminal errors stay outside the payload queue and must not be erased.
- Automatic diagnostics omit argv, environment values and captured payloads unless that client enables `raw_diagnostics=True`. Safe inspect failures dump no response and suppress input-bearing causes. Raw fetch validation re-raises `ValidationError`; raw model validation wraps it in `ParsingError`, chains it and dumps the response.
- Raw `docker_command`/`stdout`/`stderr` attributes remain confidential and intact; explicit output, uncaptured terminal output and caller logging are outside this policy.

## JSON Utility

- `_json.py` native `dumps`/`loads` use orjson; compat uses stdlib, never fallback. Native dumps compact bytes, maps nonfinite floats to null, rejects oversized integers/lone surrogates; loads rejects nonfinite tokens and may decode oversized integers as floats.
- `dumps_compat`/`loads_compat` retain stdlib numbers/errors; dumps ASCII-escaped text with `sort_keys`, `indent`, `separators`, `allow_nan`. CLI decoders, canonical hashes, raw diagnostic files, delivery REST and benchmark retain compat bytes and JSON-mode Pydantic validation.
- Fleet contract/harness stay stdlib-only; isolated distribution smoke stays checkout-independent. Fixture oracles and Pydantic JSON methods remain unchanged.

## Telemetry

- This is telemetry's only guide: `telemetry/_records.py` defines schema-8 `LaunchRecord`, `CompletionRecord`, `RefusalRecord`, `AdmissionDecisionRecord`, their `TelemetryRecord` union and `TelemetryObserver`; `_lifecycle.py` owns `ExecutionTelemetry`/`deliver()`; `_context.py` owns admission correlation; `_exporter.py` owns `BoundedExporter`.
- Execution records contain only fixed family (`captured`, `stream`, `archive_load`, `archive_save`), opaque attempt ID, optional launch or refusal admission ID, and the applicable closed outcome. Completion alone has duration, byte counts and return code; refusal has none. Admission decisions contain only opaque admission ID, closed outcome, and nonnegative activation-to-decision duration plus version/phase. Never add argv, endpoint aliases, pressure-class names, callables, environment, output or exception text, even in raw mode.
- `ExecutionTelemetry.start()` and `preflight()` stamp launch or refusal admission IDs from telemetry's context variable: `None` outside observed admission, innermost ID inside it. Completion joins launch by `attempt_id`, not a second admission field. Core launch modules must not import `pywhales.admission`; scope and decision-lock rules belong to [the admission guide](admission/AGENTS.md).
- Snapshot observer with the execution policy; none means no telemetry clock, ID or callback. `start()` follows preflight; an observation exception from that check emits one refusal (`cancelled`, `timeout`, or `limit`) and no launch/completion. Earlier validation remains outside the contract. `finish()` publishes only the first completion. Ordinary observer exceptions are swallowed.
- Execution outcomes: `success`, `failure`, `timeout`, `closed`, `cancelled`, `limit`. Shared `outcome_of()` maps Docker/observation timeout, observation cancellation/limit, and `GeneratorExit` respectively; all others, including `KeyboardInterrupt`/`BufferError`, are failure. Admission outcomes are separate: `granted`, `busy`, `queue_full`, `pending_demand_full`, `owner_pending_demand_full`, `closed`, `queue_timeout`, `cancelled`, `failed`; a grant says nothing about later workload success.
- Captured runs, archives, stream launch and delivered stream errors share that map; a `native_argv` failure after `start()` retains its launch and `limit` completion, not a refusal record.
- `ProcessStream._stop_outcome()` follows delivery order: expired, scope error, reader error. Terminal delivery and close agree; close without a stop is `closed`, while a token discovered during close retains its outcome.
- `BoundedExporter` creates no worker/timer/network: enqueue or drop newest; caller-thread `flush()`/`shutdown()` export a finite snapshot, counting sink failures without retry.
