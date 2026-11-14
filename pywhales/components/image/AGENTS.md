# Image Component Guide

## Local Navigation

- `cli_wrapper.py`: `Image`, `ImageCLI`, batch inspection, transfers, copy helpers, and archives.
- `models.py`: inspect snapshots; `config` and `container_config` reuse `ContainerConfig`,
  while `ImageGraphDriver.data` accepts arbitrary JSON values, including scalars and null.
  `ImageHistoryLayer` and `ImageSearchResult` model `{{json .}}` records; Docker prints their sizes,
  counts and flags as strings, so they rely on lax coercion.
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
- `all_tags=True` takes one `str` repository with no tag or digest (`ValueError` otherwise, after the `timeout` and
  `quiet`/`stream_logs` checks and before the byte-budget snapshot, reference consumption or lookup). It is a single
  transfer: no fanout, no handles, `None` when blocking and the stream when streaming. Push skips its batch inspection,
  because a bare repository isn't an image reference and `inspect` would test `repo:latest`; the CLI reports a missing
  repository. Iterable and empty requests keep their results without `all_tags`, but `all_tags=True` rejects them.
- Push `platform` is one `str` forwarded to every transfer, including each fanout task and streamed child. The push
  preflight inspection never carries it. Pull's `platform` keeps its `--platform=value` token and empty-string skip.
- Private `_pull_single_tag`/`_push_single_tag` calls always forward the new options, so tests that pin their keyword set
  must name them.

## Multi-Image Streams

- `MultiImageStream` starts workers on first consumption, not construction or context entry.
  It owns active child streams and sizes workers then from the remaining child allowance in its snapshotted creation context, capped at four.
  Each worker enters a fresh copy of that context, so children keep the creator's
  observation scope even if iteration starts after its `with` block.
- Both fanouts use `ObservationState.child_allowance()`, including occupied outer accounts. Unobserved or child-unbounded scopes keep four workers; one slot serializes transfers. Zero allowance starts one worker so normal preflight refuses without consuming an attempt. Sizing reserves nothing; later sibling contention still refuses immediately rather than waiting.
- Group workers are daemon threads the group starts and joins itself, not a `ThreadPoolExecutor`, so an unclosed group never delays interpreter exit. A dropped group is closed by the shared reclaimer ([runtime guide](../../AGENTS.md#observation-and-subprocesses)); `tests/pywhales/components/test_image_reclaim.py` covers both, including a subprocess exit check.
- A single unique reference uses an eager `MappedProcessStream`, including duplicate-only input.
- Records are `(image_reference, bytes)`; don't promise global ordering across images or pipes.
- The first worker error, a child's `DockerTimeout` included, stops sibling work and queued transfers without
  waiting for consumption. Deliver that original error once through iteration, including after explicit `close()`.
- Snapshot the selected byte budget, including `None`, for the group queue and every eager, lazy, or queued
  child stream. Later budget mutations affect new public operations only. Pass `None` explicitly at
  the shared boundary so it stays unbounded. A record exceeding a configured budget raises `BufferError`.

## Inspection, Copy, And Archives

- `list(hydrate=True)` deduplicates full IDs in first-seen CLI order and pins snapshots through `_hydration.py`; the default list retains unordered set-based deduplication and lazy handles. Image `tag()` and `remove()` release supplied handles' pins even when their launch fails.
- Batch inspect must verify each snapshot's ID or normalized tag/digest aliases against its request.
  Positional pairing alone isn't identity validation; mismatches raise `ParsingError`.
- Normalization handles Docker Hub/library prefixes, implicit `:latest`, and tagged digest references.
  Keep typed inspect snapshots on returned images instead of fetching them again immediately.
- `copy_from()` copies through a temporary container; `copy_to()` copies into one and commits it,
  returning a new `Image` with optional `new_tag`. Both forward `pull`, defaulting to `"missing"`.
  Their `timeout` times each launch they make (inspect through removal) via `execution_timeout()`, not config mutation.
- `save()` rejects empty input and inspects references before saving. An output path returns `None`;
  omitted output returns an iterator of archive bytes. Don't treat it as a progress-log stream.
- `platform` on `load()`, `save()` and `remove()` is `str | Iterable[str] | None`, emitted through
  `_add_platforms` as repeated `--platform` options (Docker's `strings` flags also take a comma list in one value) and
  only when not `None`, so existing argv is unchanged. `Image.save()` and `Image.remove()` forward it. The save
  preflight inspection and archive deadlines, reservations and accounts are untouched by it.
- `inspect(platform=...)` takes one `str` and returns `ImageInspectResult` snapshots (a single one for a string, a list
  for an iterable), never handles: the daemon reports the platform manifest's own ID, which `image inspect` can't find
  again, so a handle would fail on its first reload. Both paths share `_inspect_snapshots`, so batch identity
  validation (ID or normalized tag/digest aliases) is identical; platform snapshots keep the image's tags and digests,
  but an image-ID reference can't match and raises `ParsingError`. Empty iterables still skip the CLI.
- `list(tree=True)` returns the CLI's experimental text unparsed and builds no handles, so it rejects `hydrate=True`
  (`ValueError`) and never seeds or pins caches. Docker refuses `--quiet`, `--no-trunc` and `--format` with `--tree`,
  so it emits only `--tree`, `--all`, filters and the selector. The default path's `--quiet --no-trunc` argv is unchanged.

## History And Search

- `history(image, *, platform=None, timeout=None)` and `Image.history()` run `image history --format "{{json .}}"
  --no-trunc --human=false`; sizes are integer bytes and Docker's `<missing>` layer ID becomes `None`.
- `search(term, *, filters=None, limit=None, timeout=None)` runs top-level `docker search --format "{{json .}}"
  --no-trunc`. Filters follow `format_filters_for_cli` (mapping or string pairs, `TypeError` before lookup); typed
  keys are `is-official` and `stars`, because `is-automated` is deprecated. The call needs registry access.
- Both validate `timeout` before filters or lookup and time their single launch. Malformed records raise `ParsingError`
  through `JsonOutputParser` without payload text; a missing history image is `NoSuchImage`.
- Byte-input `load()` and lazy `save()` launch through `_prepare_archive_launch`, which validates the selected
  timeout (explicit, else the client default) before any process starts or input is read. A save's eager
  inspection stays outside the save deadline: it runs in an `execution_timeout()` scope that ends before any
  yield. A lazy save's clock starts at first iteration, using the client default as configured then.
- Direct archives carry the preflight child reservation into `ArchiveDeadline`: release once at local reap,
  independently of payload-drain completion or generator exhaustion, or after failed setup leaves no owned child. Attempts stay
  consumed. A child-only scope retains ownership without a timed wait; lazy save reserves on first iteration.
- Direct byte-input loads and lazy saves share enclosing `max_total_io_bytes` accounts with captured/stream output and managed stdin, including copied contexts and after scope exit. Charge output reads and accepted upload writes atomically; exact EOF succeeds, output may read one detection byte, and source limits win ties. Legacy cleanup drains and post-exit stderr accounting stay outside the total, preserving `NoSuchImage`; enabled diagnostic stderr uses its separate allowance below. Filename payloads and direct caller stdin writes are excluded; see [the runtime guide](../../AGENTS.md#observation-and-subprocesses).
- `InspectionPolicy.max_total_archive_stderr_bytes` opts direct byte/iterator load and lazy save into independent diagnostic readers. Load captures stderr and drains stdout concurrently with upload; save drains stderr while stdout consumption is paused. Nested/copied accounts charge actual stderr reads, including post-reap, separately from stream/aggregate I/O budgets. A positive exact integer is required, never bool; `None` preserves legacy behavior unless an outer scope supplies a bound. Lazy save selects the allowance at first iteration and retains it after scope exit.
- Exact diagnostic EOF succeeds; one shared detection byte triggers `ObservationLimitError("total_archive_stderr")`, with no later diagnostic read or retention of that byte. Cleanup reaps the local child, closes pipes and joins workers. Overflow precedes classifying truncated stderr; below the bound load keeps raw stderr on `DockerException`, and save preserves `NoSuchImage` and its raw stderr. Telemetry uses `limit`/`other_limit`, with no new schema field or reason. This doesn't interrupt caller iterator code, roll back daemon work, or impose a payload/total-memory cap; filename paths keep their existing execution families and budgets.
- Path-based `load()` uses the general stream budget, not the pull/push override.
  Direct archive load/save subprocesses snapshot `raw_diagnostics` before launch for failure rendering;
  preserve archive payloads, exception classes, and raw attributes. Don't promise archive memory bounds.
- Automatic missing-image reference diagnostics require `raw_diagnostics=True`; fallback behavior is unchanged.
- Observation exceptions from `_prepare_archive_launch` preflight emit one schema-11 refusal with a closed `stop_reason` in family `archive_load` or `archive_save`, before a child or upload read. Lazy save checks at first iteration with its policy-snapshotted observer; path-based load/save keep their existing shared-runtime families without duplicate records. Earlier validation stays outside refusal telemetry; see [the runtime guide](../../AGENTS.md#telemetry).

## Prune Deadline

- `prune()` retains its `str` result and positional `filters`/`all`; keyword-only `timeout` validates before filters or executable lookup. `None` inherits the launch-snapshotted client default; explicit values override without config mutation. Preserve pair-filter order and mapping rejection, and never add a Docker timeout flag or retry after an ambiguous local timeout. Shared deadline tests cover partial output, reaping, defaults, invalid inputs and natural failures.

## Test Navigation

- `tests/pywhales/test_container_image_enum_json.py`: nested config and graph-driver JSON contracts.
- `tests/pywhales/components/test_image_child_limits.py`: barrier/event fake children prove blocking and streamed pull/push fanout, occupied nested accounts, late iteration, zero-allowance refusal without debit, and sibling races. These tests start no processes; `test_image.py` retains real-child lifecycle coverage and fake-CLI hydrated listing cases.
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
- Diagnostic allowance cases in `tests/pywhales/test_inventory_types.py`, `test_observation.py`, `test_bounded_capture.py`, `test_telemetry_archive_lifecycle.py`, and `components/test_image.py` cover validation, nested/copied atomic reads, exact EOF/post-reap overflow, noisy upload/paused-save progress, retained scopes, raw failures and worker cleanup. `test_telemetry_stop_reasons.py` keeps `total_archive_stderr` mapped to `other_limit`.
- Copy forwarding uses `test_copy_from_pull` and `test_copy_to_pull`; archive integration includes `test_save_load`
  and `test_save_iterator_bytes`. Read fixtures before choosing daemon-free selections.
- `tests/pywhales/components/test_image_cli29.py` is daemon-free: a recording fake CLI (`argv_fakes.py`) checks exact
  argv, validation before lookup, parse errors and a real-child deadline for history, search, tree listing, `all_tags`
  and `platform`. Its fixtures are `jsons/image_history/` and `jsons/image_search/`; static shapes are in
  `tests/pywhales/test_image_cli29_types.py`.
