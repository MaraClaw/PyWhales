# Buildx and imagetools

This guide also applies to `imagetools/`; paths below are relative to this directory unless prefixed with `tests/`.
`cli_wrapper.py` holds `BuildxCLI`, `Builder`, `stream_buildx_logs()`, `parse_builder_listing()`, and
`list_builder_snapshots()`; `models.py` holds builder snapshots.

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
- Build streaming rejects `progress=False`, `"tty"`, `"none"`, and `"quiet"`.
- Every build launch branch passes the snapshot-bound child environment returned by the shared
  `container_environment` eligibility seam for `build_args`. Strict fallback rejection precedes
  lookup and builder inspect; compatibility mode keeps literal fallback. Bake `--set` is unchanged.

## Bake configuration

- A normal blocking bake returns `BakeResult` by default: it builds, then runs a second command with `--print`.
  Keep options, targets, and the `variables` environment on both calls; `jobs=0` must be forwarded.
- `return_config=False` skips that second call and returns `None`; it doesn't suppress report text.
- `print=True` only queries config. It conflicts with streaming, `return_config=False`, and report calls.
- Reports return text; streams return `MappedProcessStream[str]`. Neither performs a follow-up config query.

## Local launch deadlines

- Per-launch `timeout` semantics (`None` inherits the client default) are in the components guide's Local Deadlines
  section; this lists which Buildx launches receive it. `stream_buildx_logs()` forwards it to the stream it starts.
- `build()` bounds only the `buildx build` launch; the builder inspection before it and the image inspection
  after it (recovery) stay outside the explicit budget. An expired build skips recovery and still removes the
  `--iidfile` directory. Reports, pushes, and non-loading exports are one launch and take the budget directly.
- Blocking `bake()` gives the build and the `--print` follow-up a fresh budget each, never one shared deadline.
  An expired build never starts the follow-up. `print=True`, reports, and streams are single launches.
- `prune()` checks `timeout` before reading `filters` and never forwards it as a `buildx prune --timeout` flag.

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
- `Builder` context exit calls `remove()`, which delegates to `BuildxCLI.remove()`.
  `disk_usage()` raises `NotImplementedError`; don't document it as a working command.

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
- Deadlines, daemon-free: `tests/pywhales/components/test_deadline_forwarding.py` mocks launches (for example
  `test_build_inspections_are_unbudgeted_and_iidfile_is_cleaned`); sibling `test_command_deadlines.py` uses a local CLI.
- Strict listing, all mocked: `test_builder_*` in `tests/pywhales/test_inventory.py`, plus builder cases in
  `tests/pywhales/test_bulk_reload.py` and `test_bulk_reload_bounded.py`.
- `tests/pywhales/test_buildx_modern_types.py`, `test_command_deadline_types.py`, and `test_type_contracts.py`:
  static return-type and deadline contracts, including dynamic flags and aliases.
- Under `tests/pywhales/components/buildx/`: `imagetools/test_contracts.py` (mocked create defaults),
  `imagetools/test_models.py` (manifest fixtures), and the integration files `test_buildx_cli_wrapper.py`
  and `imagetools/test_imagetools_cli_wrapper.py`. Global-client builder switches and pruning follow `tests/AGENTS.md`.
