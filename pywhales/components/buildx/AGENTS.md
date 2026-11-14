# Buildx and imagetools

This guide also applies to `imagetools/`; paths below are relative to this directory unless prefixed with `tests/`.
`cli_wrapper.py` holds `BuildxCLI`, `Builder`, `stream_buildx_logs()`, `parse_builder_listing()`, and
`list_builder_snapshots()`; `models.py` holds builder snapshots and `BuildxDiskUsage`; `dap.py` holds
`BuildxDapCLI`, wired as `docker.buildx.dap`.

## Build returns and aliases

- `docker.build()` and `docker.image.build()` route to `BuildxCLI.build()`. Keep their overloads aligned.
- Normal blocking builds (`call=None` or `"build"`) return one `Image` only when the result is
  loaded into the daemon and can be identified; otherwise they return `None`.
- `_build_will_load_image()` checks `load=True` (load), then `push=True` (no load), then `output`:
  only `type="docker"` without `dest` loads. With no explicit choice, driver `"docker"` loads by default.
- Driver `"docker"` recovers the immutable image ID through a temporary `--iidfile`.
  Other drivers recover loaded images through the first tag. Untagged `"docker-container"` loads return `None`.
- Blocking `call="check"`, `"outline"`, or `"targets"` returns captured report text and skips builder/image recovery.
- `stream_logs=True` returns `MappedProcessStream[str]` before recovery or report capture.
  `stream_buildx_logs()` decodes `(source, bytes)` tuples with replacement and discards the source labels.
  The mapped helper inherits the originating client's general `stream_buffer_bytes` at the shared
  stream boundary; keep its policy carrier and transport ownership through mapping.
- Build streaming rejects `progress=False`, `"tty"`, `"none"`, and `"quiet"`, and rejects `quiet=True` with a
  value-free `ValueError` before any lookup.
- `_build_command()` in `cli_wrapper.py` is the one private assembler for `build` and `dap build`; they share
  option values, order, and the build-argument environment seam. `build()` argv is pinned byte for byte by a
  snapshot in `tests/pywhales/test_buildx_core_cli29.py`; append new options after the existing ones, and
  put `--debug` immediately after the subcommand path.
- `cgroup_parent`, `policies`, `quiet`, `resources` (repeated `--resource key=value`), `shm_size` and `ulimits`
  default to omitted. `dap build` options are identical to `build` options; it adds only `iidfile`.
- Every build launch branch passes the snapshot-bound child environment returned by the shared
  `container_environment` eligibility seam for `build_args`. Strict fallback rejection precedes
  lookup and builder inspect; compatibility mode keeps literal fallback. Bake `--set` is unchanged.

## Bake configuration

- A normal blocking bake returns `BakeResult` by default: it builds, then runs a second command with `--print`.
  Keep options, targets, and the `variables` environment on both calls; `jobs=0` must be forwarded.
- `return_config=False` skips that second call and returns `None`; it doesn't suppress report text.
- `print=True` only queries config. It conflicts with streaming, `return_config=False`, and report calls.
- Reports return text; streams return `MappedProcessStream[str]`. Neither performs a follow-up config query.
- `list="targets"` or `"variables"` (`--list`) is a report: one launch, table text (or a stream), no config query,
  `return_config` ignored. The CLI rejects it with `--print`, so the wrapper raises before any launch.
- `variables` sets the launch environment; `variables_set` emits repeated `--var name=value`. `provenance` and
  `sbom` encode like build (`--provenance=true` for booleans, `key=value` pairs for mappings).

## Local launch deadlines

- Per-launch `timeout` semantics (`None` inherits the client default) are in the components guide's Local Deadlines
  section; this lists which Buildx launches receive it. `stream_buildx_logs()` forwards it to the stream it starts.
- `build()` bounds only the `buildx build` launch; the builder inspection before it and the image inspection
  after it (recovery) stay outside the explicit budget. An expired build skips recovery and still removes the
  `--iidfile` directory. Reports, pushes, and non-loading exports are one launch and take the budget directly.
- Blocking `bake()` gives the build and the `--print` follow-up a fresh budget each, never one shared deadline.
  An expired build never starts the follow-up. `print=True`, reports, and streams are single launches.
- `prune()` checks `timeout` before reading `filters` and never forwards it as a `buildx prune --timeout` flag.
- Docker's own `--timeout` (builder status loading, default 20s) is `status_timeout` (whole seconds or a
  `timedelta`, via `to_seconds`) on create, inspect, list, prune, remove and `disk_usage`. Never name it
  `timeout`; `inspect()` also forwards it to its follow-up listing.
- `disk_usage()`, `dap.build()` and `dial_stdio()` take the local `timeout`, validated before any lookup.
  The last two run with inherited caller stdio (`run(..., tty=True)`), block until the CLI ends and return
  `None`; no process stream is offered because DAP and gRPC traffic is not line-delimited. Expiry kills and
  reaps only the local CLI.

## Debug logging and raw text

- Every `BuildxCLI` method except `is_installed()`, plus `Builder.remove()` and `dap.build()`, takes keyword-only
  `debug: bool = False`, emitted as `--debug` right after the subcommand path. It never changes a return type,
  except on the typed reads below.
- `list()`, `inspect()` and `disk_usage()` return `str` for `debug=True` (and `verbose=True` for
  `disk_usage`): stdout, a newline, then the captured stderr debug log; interleaving is not preserved. The
  internal `--format` is dropped, so `list(debug=True)` is the human table, nothing is parsed, and no handle
  is seeded or pinned. `disk_usage(verbose=True)` alone returns stdout only.
- `disk_usage()` reads one JSON object per line (`buildx du --format json`) into `BuildxDiskUsage`; every
  field is optional and sizes and times keep the CLI's display strings.

## History, policy and replay

- `history.py` (`BuildxHistoryCLI`, models in `history_models.py`), `policy.py` (`BuildxPolicyCLI`) and
  `replay.py` (`BuildxReplayCLI`) are wired as `docker.buildx.history`, `.policy` and `.replay`, sharing the
  client config. Tests: `tests/pywhales/test_buildx_history_policy.py` and its `_types.py` contracts.
- `history.trace()` returns an owned stream. A local deadline or `close()` reaps only the local `docker` CLI;
  in `compare` mode the `docker-buildx` plugin can keep serving, as live checks showed. Keep that caveat in
  the docstring.
- `history.open()` launches Docker Desktop's build UI and `history.import_()` writes to Desktop's build history.
  Only fake-CLI tests cover them; don't add live tests that run either one.

## Builder snapshots

- `Builder` reloads by matching its name in line-delimited JSON from `buildx ls --format "{{json . }}"`.
- Public `list()` keeps its lenient contract: lines pass through a `set` (existing dedupe, unspecified order),
  parse with `json.loads`, and seed one handle each. Don't route it through the strict parser or sort it.
  `inspect()` reads the name from the first human-readable output line, then finds the builder through `list()`.
- `parse_builder_listing()` and `list_builder_snapshots()` are the strict path, separate from public `list()`.
  Every line must validate as a strict `BuilderInspectResult`. Byte-identical lines collapse (node rows repeat
  them); malformed lines, null or empty names, and a different line under one name raise `ParsingError` before
  any snapshot returns.
- `list_builder_snapshots()` runs one listing with the client's `raw_diagnostics`. Inventory capture and
  `bulk_reload()` reach it through `_bulk_reload._inspect_builders`, once per config group: names match exactly
  and never reach argv, each request gets its own deep-copied snapshot, and an absent name raises `ParsingError`.
- `Builder` context exit calls `remove()` with no options, which delegates to `BuildxCLI.remove()`;
  the handle's `remove()` also forwards `force`, `keep_daemon`, `keep_state`, `status_timeout` and `debug`.
- `BuildxCLI.remove()` takes an optional builder: none removes the current builder. `all_inactive` conflicts
  with a builder (`ValueError` before any launch). Without `force` the CLI may prompt on inherited stdin.
- `create()` emits `--buildkitd-config` for its `config` parameter (the old `--config` is deprecated), plus
  `leave`, `node` and `status_timeout`.

## Registry manifests

- Imagetools works on registry manifests, not local `Image` objects; inspecting doesn't pull an image.
- `imagetools/cli_wrapper.py::inspect()` runs `imagetools inspect --raw` and parses a `Manifest`.
- `create()` requires sequences for `sources`, `tags`, and `files`; a bare string is rejected.
  `annotations` must be a mapping. Sources already exist in the registry.
- `dry_run=True` parses the proposed manifest into `Manifest`; normal create returns `None`.
- `imagetools/models.py` supports a manifest's `config`/`layers` and an index's `manifests` variants,
  with explicit `mediaType` and `schemaVersion` aliases.

## Related tests

- `tests/pywhales/test_buildx_modernization.py`: mocked build aliases, recovery, reports, and bake command counts.
- `tests/pywhales/test_buildx_core_cli29.py` and `test_buildx_core_cli29_types.py`: fake-CLI argv, the `build()`
  snapshot, debug text modes, disk-usage fixtures (`components/jsons/buildx_du/`), interactive commands and
  their static contracts.
- Deadlines, daemon-free: `tests/pywhales/components/test_deadline_forwarding.py` mocks launches (for example
  `test_build_inspections_are_unbudgeted_and_iidfile_is_cleaned`); sibling `test_command_deadlines.py` uses a local CLI.
- Strict listing, all mocked: `test_builder_*` in `tests/pywhales/test_inventory.py`, plus builder cases in
  `tests/pywhales/test_bulk_reload.py` and `test_bulk_reload_bounded.py`.
- `tests/pywhales/test_buildx_modern_types.py`, `test_command_deadline_types.py`, and `test_type_contracts.py`:
  static return-type and deadline contracts, including dynamic flags and aliases.
- Under `tests/pywhales/components/buildx/`: `imagetools/test_contracts.py` (mocked create defaults),
  `imagetools/test_models.py` (manifest fixtures), and the integration files `test_buildx_cli_wrapper.py`
  and `imagetools/test_imagetools_cli_wrapper.py`. Global-client builder switches and pruning follow `tests/AGENTS.md`.
