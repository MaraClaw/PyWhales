# Compose Contracts

`cli_wrapper.py` defines `ComposeCLI`; `models.py` holds configuration, project, event and listing models;
`bridge.py` holds the Compose Bridge sub-CLI, wired as `docker.compose.bridge`.
Build progress overrides and Compose globals follow [the package guide](../../AGENTS.md).
`run`/`execute` `envs` and `build` `build_args` follow its [shared environment transport](../../AGENTS.md#container-environment-transport), including the interpolation caveat.

## Configuration Models

- Compose models inherit ordinary Pydantic `BaseModel`, not `DockerCamelModel`.
- `config()` returns `ComposeConfig`; `config(return_json=True)` returns
  `dict[str, JsonValue]`, preserving fields that aren't declared in the typed model.
- Keep nested JSON fields typed as `JsonValue`, including scalar/list values where allowed.
- Output enums retain `EnumType | str | None` so unfamiliar wire strings survive parsing.
- Preserve `Field(default_factory=...)` declarations and independent mutable defaults.
- `ComposeImage`, `ComposeVolume`, `ComposeStats`, `ComposeVariable` and `ComposeTransformation` model the CLI's capitalized
  JSON through explicit `Field(alias=...)` on `_ComposeWireModel` (`populate_by_name`), not `DockerCamelModel`. Every field is
  optional. Volume and stats values keep the CLI's display text (`N/A`, `0.00%`, `0B / 0B`). `ComposeStats` has no service
  field because the wire has none; `pids` is a string per container and an integer for `all_projects`/`aggregate` rows, and
  `containers` is set only for aggregate rows.
- Their fixtures are `tests/pywhales/components/jsons/compose_{images,volumes,stats}/`. Volumes and stats are
  newline-delimited on the wire, so each file holds one record and tests join them; images hold the array or `null`.

## Service Selection

- `None` selects all services and `[]` skips execution for `build`, `create`, `down`,
  `kill`, `pause`, `pull`, `push`, `restart`, `rm`, `start`, `stop`, `unpause`, `up`, and `watch`.
- `top([])` returns an empty `ComposeTopResult`, or `""` with `raw=True`, without CLI lookup.
- Don't generalize empty-selection behavior: `ps([])`, `logs([])`, and `events([])` select all.
- The read-only listings `images`, `volumes` and `config_hashes` also treat `None` and `[]` as all services. Mutating
  `scale({})` is a no-op that validates `timeout` first and never resolves the CLI. `wait([])` raises `ValueError` before
  lookup, because Compose requires a service. `stats()` takes at most one service; `commit`, `export` and `attach` take one.
- `config_names(kind)` accepts only the closed `ComposeConfigNames` set and raises `ValueError` before lookup, so no caller
  string reaches argv as an option.

## Execution And Returns

- `build`, `down`, `pull`, and `up` reject `quiet=True` with `stream_logs=True`.
- `build(print=True)` still returns `None` when blocking, or an output stream with `stream_logs=True`.
- Streamed `create()` prints its command only with `raw_diagnostics=True`; returned log records are unchanged.
- Record-stream branches of `build`, `create`, `down`, `logs`, `pull`, `run`, `start`, `stop`, and `up` expose owned `ProcessStream`, not generic iterables; records are `(source, bytes)`.
- `run()` otherwise returns attached `str` or detached `Container` retaining its config and complete captured ID as immutable identity, without truncation or a second lookup. Blocking and `[]` no-op `None` arms stay unchanged; guard optional streams before entering their context.
- `run()` defaults to `tty=True`. Reject TTY plus streaming, detach plus streaming,
  and detach plus TTY; callers must set `tty=False` for detached or streamed runs.
- `ps()` returns container resources, or service names with `service_names=True` (`--services`). Reject `ps()` in either
  form, `commit()` and detached `run()` in dry-run mode before execution; dry-run output holds no IDs or names. Attached
  `run(tty=False)` can return preview text instead. `ps(filters=...)` accepts at most one filter (`ValueError`), because the
  CLI keeps only the last `--filter`; `status` repeats `--status`.
- Tri-state options (`up(menu=...)`, `run(interactive=...)`, `ps(orphans=...)`, `attach(sig_proxy=...)`) omit the flag for
  `None`, send `--x` for `True` and `--x=false` for `False`; `attach` defaults `sig_proxy=True` to `--sig-proxy`.
- `exec` and `run` send the canonical `--no-tty` (not `--no-TTY`). `up(shutdown_timeout=...)` is Compose's `--timeout`
  (whole seconds, `timedelta` truncated, `0` kept), not a local deadline. `up` has no `yes`: `use_default_sensitive_values`
  already sends `--yes`.
- `commit()` always runs with `--progress plain` and ignores `compose_progress`: the new image ID exists only in the
  progress text on stderr (` <container> Committed as sha256:<64 hex>`), and quiet, tty or truncated modes hide or shorten it.
  It returns an `Image` keeping the complete ID as immutable identity; a missing full ID raises a value-free `ParsingError`.
- `wait()` returns the stopped container's exit status. Compose reports that status as its own exit code and prints a
  summary on stdout, so a nonzero exit with empty stderr is returned; any stderr (unknown service, daemon failure) raises
  `DockerException`, and `DockerTimeout` always propagates. `0` means a clean stop.
- `stats()` takes an untruncated snapshot (`--no-stream --no-trunc --format json`, parsed line by line); `stream=True` returns
  the owned raw `ProcessStream` of byte records without `--no-stream`. Each frame redraws the terminal, so records can hold
  control bytes and are never parsed; close the stream on early exit.
- `config(output=...)` and `config(quiet=True)` return `None` and parse nothing. `config_environment()` lists the whole
  inherited environment plus env files, so it can hold secrets, and can't tell multi-line values apart.
- `attach()` inherits the caller's terminal streams (`tty=True`); `cp()` takes `(service, path)` tuples as the container
  endpoint; `export()` writes to a file on the Python host (`--output`), never returns bytes.
- Non-detached `run()` (captured, TTY, or streamed) and `execute()` (non-streamed; captured or TTY) explicitly pass `workload_status=True`. With originating-client `strict_workload_errors=True`, nonzero exits raise exact `DockerException`; detached and other Compose launches retain specialization. `execute()` has no stream mode; don't add one for this policy. See [shared classification rules](../../AGENTS.md#configuration-and-policy).

## Local Deadlines

- `build`, `execute`, `run`, `pull`, `push`, `watch`, `logs`, and `events` take keyword-only `timeout`: positive finite seconds from the
  local CLI launch, or `None` to inherit `ClientConfig.default_timeout`. Expiry kills and reaps only the local CLI and
  raises `DockerTimeout`; it doesn't stop services, undo pulled or pushed layers, or change post-exit pipe draining.
  A returned stream's deadline advances while nothing consumes it.
- `build()`, `pull()`, `push()`, and `watch()` validate `timeout` before the `[]` no-op: an invalid value raises `ValueError`
  for `[]` and `None` selections alike, and a valid one with `[]` returns `None` without a launch. `pull()` validates
  before its quiet/stream conflict check. Blocking `push()` returns `None`; `pull(stream_logs=True)` returns a stream.
- `attach`, `cp`, `export`, `commit`, `scale`, `stats`, `wait`, `bridge.convert` and `bridge.transformations.create` also take
  keyword-only `timeout`, validated before lookup, the `[]`/`{}` no-ops and the dry-run check. `images`, `volumes`, `ps` and the
  `config*` queries take none and use the client default. `wait()` is unbounded without a deadline; expiry leaves containers
  running.
- Expiry kills and reaps the local `docker` CLI only. Live checks of `attach()` and `logs(follow=True)` found the
  `docker-compose` plugin child still running afterwards; don't claim plugin cleanup in docstrings or tests.
- Dry-run rejections and the `[]` rules in Service Selection are unchanged by timeouts. `watch()` keeps its explicit
  `--prune=false` or `--prune=true` under a deadline.
- Local budgets aren't Docker option values: `down()`, `restart()`, and `stop()` `timeout` is the container shutdown
  grace period, and `up(wait_timeout=...)` is Compose's own wait. Never forward one as the other.
- Read-only readiness and designated one-shot `completion_services` belong to `pywhales.convergence` (`capture_compose()`, `wait()`); follow [its guide](../../convergence/AGENTS.md).

## Logs And Events

- Logs retain collected text or an owned `(source, bytes)` stream. Events retain
  `MappedProcessStream[ComposeEvent]`, parsing stdout JSON only and owning the underlying transport.
- General `stream_buffer_bytes` is selected at the shared stream boundary, including for collected logs.
  Don't stringify away the originating policy carrier or add wrapper-specific budget defaults.

## Top And Watch

- `top()` returns `ComposeTopResult` with `raw_output` and ordered sections;
  `raw=True` returns untouched CLI text instead of parsing it.
- Grouped sections retain container names. Flat `SERVICE`/`#` tables retain attribution
  in headings/cells with `container=None`; don't apply Docker container-top grammar.
- `watch()` returns an owned `ProcessStream`, or `None` for `[]`. Use a context manager
  or explicit `close()` under the package guide's ownership contract.
- Always emit explicit `--prune=false` by default, or `--prune=true` when requested.
  Watch's `quiet=True` hides build output but remains compatible with its output stream.

## Bridge

- `bridge.py`: `ComposeBridgeCLI` is `docker.compose.bridge`; it holds `convert()` and the `transformations` group
  (`list()`, `create()`). Both use the shared Compose globals and need an installed plugin; nothing probes its version.
- `convert()` inherits stdout and stderr (progress and the overwrite prompt reach the caller) and repeats
  `--transformation`; `yes=True` answers that prompt for scripts. `transformations.list()` runs `--format json` and returns
  `ComposeTransformation` summaries, treating `null` as empty; `create(path, from_=...)` writes the new transformation to a
  directory on the Python host.

## Compose Globals

- `ClientConfig.compose_all_resources` (`--all-resources`) and `compose_ansi` (`--ansi never|always|auto`) are appended after
  `inherited_environment_names`, emitted by `get_compose_command()` after `--progress`, and take part in config equality.
  Both default to omitted, so default argv is unchanged.

## Regression References

Paths below are relative to `tests/pywhales/`; collection/runtime cautions are in [the test guide](../../../tests/AGENTS.md).
- `test_compose_cli29.py` and `test_compose_cli29_types.py`: fake-CLI argv for the Compose v5.7.1 commands, options, Bridge
  and globals; empty-selection and validation-before-lookup rules; `wait`, `commit` and listing parsers; keyword-only shapes.
- `test_compose_modernization.py`: argv, per-build progress, empty selection, dry-run resources, watch ownership.
- `test_compose_build_enum_json.py`: nested JSON, enum/string outputs, independent defaults, raw config.
- `test_exceptions.py`: strict-workload marks on non-detached `run`/`execute`, with detached classification controls; shared captured and stream exception paths are covered in `test_utils.py` too.
- `components/test_compose.py`: top layouts/raw/no-op/error cases, logs/events ownership, legacy integration.
- `components/test_command_deadlines.py` and `components/test_deadline_forwarding.py`: run/log/event/pull/push/watch
  deadline execution, forwarding, and empty-selection validation.
- Static contracts: `test_compose_type_contracts.py`, `test_type_contracts.py` (`_check_owned_stream_contracts`),
  `test_command_deadline_types.py`, and `test_enum_json_type_contracts.py`.
