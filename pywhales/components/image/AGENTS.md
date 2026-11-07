# Image Component Guide

## Local Navigation

- `cli_wrapper.py`: `Image`, `ImageCLI`, batch inspection, transfers, copy helpers, and archives.
- `models.py`: inspect snapshots; `config` and `container_config` reuse `ContainerConfig`,
  while `ImageGraphDriver.data` accepts arbitrary JSON values, including scalars and null.
- `streams.py`: `MultiImageStream`, plus the `ImageStream` and `EmptyImageStream` type aliases.

## Pull And Push Contracts

- Deduplicate iterable references in first-seen order before choosing single or multiple transfers.
  An iterable pull still returns a list without streaming, even with one unique reference.
- `quiet=True` with `stream_logs=True` raises `ValueError` for both pull and push.
- Nonstreamed transfers are eager; multiple unique references use a `ThreadPool` sized to the caller's remaining child allowance, capped at `WORKERS` (4),
  through `streams.py::map_in_contexts`. Each task enters its own `copy_context()` made in the
  caller's thread; never share one `Context` across workers.
  Pull returns `Image` for a string or an ordered list for an iterable; push returns `None`.
- Inside `observation()`, only nonstreamed transfers reject `quiet=False`: `run()` raises `ValueError`
  for one reference or many. `stream_logs=True` accepts `quiet=False` there.
- `pull([], stream_logs=True)` returns owned `EmptyImageStream`: empty iteration, context-manager support and idempotent `close()`, without executable lookup or a child.
  Blocking `pull([])` stays `[]`; `push([])` stays `None` even with streaming enabled. Preserve this asymmetry and the `ImageStream`/`EmptyImageStream` type aliases.
- Validate `timeout` first: before references are consumed (strings, iterators, and `[]` alike), before
  push inspection, and before any CLI lookup. Empty requests keep their results after a valid timeout.
- Each transfer is timed from its own launch: every pool task, and every lazy or queued stream child once a
  worker starts it, never from queueing or group creation. Push batch-inspects deduplicated references before
  any transfer, streamed ones included; that inspection and a pull's handle inspection sit outside the deadline.
- Snapshot the selected byte budget (image override, else general) once at public `pull()`/`push()` entry,
  before consuming references or running push preflight.

## Multi-Image Streams

- `MultiImageStream` starts workers on first consumption, not construction or context entry.
  It owns active child streams and sizes workers then from the remaining child allowance in its snapshotted creation context, capped at four.
  Each worker enters a fresh copy of that context, so children keep the creator's
  observation scope even if iteration starts after its `with` block.
- Both fanouts use `ObservationState.child_allowance()`, including occupied outer accounts. Unobserved or child-unbounded scopes keep four workers; one slot serializes transfers. Zero allowance starts one worker so normal preflight refuses without consuming an attempt. Sizing reserves nothing; later sibling contention still refuses immediately rather than waiting.
- A single unique reference uses an eager `MappedProcessStream`, including duplicate-only input.
- Records are `(image_reference, bytes)`; don't promise global ordering across images or pipes.
- The first worker error, a child's `DockerTimeout` included, stops sibling work and queued transfers without
  waiting for consumption. Deliver that original error once through iteration, including after explicit `close()`.
- Snapshot the selected byte budget, including `None`, for the group queue and every eager, lazy, or queued
  child stream. Later budget mutations affect new public operations only. Pass `None` explicitly at
  the shared boundary so it stays unbounded. A record exceeding a configured budget raises `BufferError`.

## Inspection, Copy, And Archives

- Batch inspect must verify each snapshot's ID or normalized tag/digest aliases against its request.
  Positional pairing alone isn't identity validation; mismatches raise `ParsingError`.
- Normalization handles Docker Hub/library prefixes, implicit `:latest`, and tagged digest references.
  Keep typed inspect snapshots on returned images instead of fetching them again immediately.
- `copy_from()` copies through a temporary container; `copy_to()` copies into one and commits it,
  returning a new `Image` with optional `new_tag`. Both forward `pull`, defaulting to `"missing"`.
  Their `timeout` times each launch they make (inspect through removal) via `execution_timeout()`, not config mutation.
- `save()` rejects empty input and inspects references before saving. An output path returns `None`;
  omitted output returns an iterator of archive bytes. Don't treat it as a progress-log stream.
- Byte-input `load()` and lazy `save()` launch through `_prepare_archive_launch`, which validates the selected
  timeout (explicit, else the client default) before any process starts or input is read. A save's eager
  inspection stays outside the save deadline: it runs in an `execution_timeout()` scope that ends before any
  yield. A lazy save's clock starts at first iteration, using the client default as configured then.
- Direct archives carry the preflight child reservation into `ArchiveDeadline`: release once at local reap,
  independently of payload-drain completion or generator exhaustion, or after failed setup leaves no owned child. Attempts stay
  consumed. A child-only scope retains ownership without a timed wait; lazy save reserves on first iteration.
- Direct byte-input loads and lazy saves share enclosing `max_total_io_bytes` accounts with captured/stream output and managed stdin, including copied contexts and after scope exit. Charge actual caller reads and accepted upload writes atomically; exact EOF succeeds, output may read one detection byte, and source limits win ties. Cleanup drains and post-exit stderr accounting stay outside the total, preserving `NoSuchImage`. Filename payloads and direct caller stdin writes are excluded; see [the runtime guide](../../AGENTS.md#observation-and-subprocesses).
- Path-based `load()` uses the general stream budget, not the pull/push override.
  Direct archive load/save subprocesses snapshot `raw_diagnostics` before launch for failure rendering;
  preserve archive payloads, exception classes, and raw attributes. Don't promise archive memory bounds.
- Automatic missing-image reference diagnostics require `raw_diagnostics=True`; fallback behavior is unchanged.
- Observation exceptions from `_prepare_archive_launch` preflight emit one schema-10 refusal with a closed `stop_reason` in family `archive_load` or `archive_save`, before a child or upload read. Lazy save checks at first iteration with its policy-snapshotted observer; path-based load/save keep their existing shared-runtime families without duplicate records. Earlier validation stays outside refusal telemetry; see [the runtime guide](../../AGENTS.md#telemetry).

## Prune Deadline

- `prune()` retains its `str` result and positional `filters`/`all`; keyword-only `timeout` validates before filters or executable lookup. `None` inherits the launch-snapshotted client default; explicit values override without config mutation. Preserve pair-filter order and mapping rejection, and never add a Docker timeout flag or retry after an ambiguous local timeout. Shared deadline tests cover partial output, reaping, defaults, invalid inputs and natural failures.

## Test Navigation

- `tests/pywhales/test_container_image_enum_json.py`: nested config and graph-driver JSON contracts.
- `tests/pywhales/components/test_image.py` contains mocked CLI and real child-process stream tests:
  `test_empty_image_operations_keep_existing_result_shapes`, `test_empty_pull_stream_is_owned_without_executable_resolution`,
  `test_multiple_image_stream_has_lazy_owned_lifecycle`,
  `test_multiple_image_stream_keeps_first_original_error`, `test_batch_inspect_image_aliases_and_typed_snapshot`.
- The same file covers fallback/override snapshots before preflight, lazy queued children, backpressure,
  path-load budgets, and safe/raw archive failures. Deadlines: `test_archive_lazy_save_explicit_timeout_starts_at_first_iteration`,
  `test_multi_image_timeout_is_launch_relative_not_group_relative`, `test_image_save_bounds_eager_inspection_with_override`,
  and `test_unquiet_transfer_in_a_scope_raises_the_same_error_for_one_or_many`.
- Daemon-free transfer deadlines: `tests/pywhales/components/test_deadline_forwarding.py` (mocked, e.g.
  `test_image_push_validates_before_consuming_or_inspecting`) and `test_command_deadlines.py` (local Python CLI,
  e.g. `test_push_inspect_preflight_is_outside_the_explicit_budget`). Static shapes: `tests/pywhales/test_command_deadline_types.py`.
- `tests/pywhales/test_bounded_capture.py` and `test_telemetry_refusals.py` cover archive child-slot lifetimes,
  reap/drain independence, failed setup and saturation refusals with real local Python children, not Docker.
- `test_bounded_capture.py` also covers aggregate I/O exact fits, detection bytes, partial uploads, retained scopes, source precedence, uncharged drains/stderr and contention across four execution families. `test_telemetry_stop_runtime.py` covers command/scope deadline reasons for both direct archive families.
- Copy forwarding uses `test_copy_from_pull` and `test_copy_to_pull`; archive integration includes `test_save_load`
  and `test_save_iterator_bytes`. Read fixtures before choosing daemon-free selections.
