# Compose Contracts

`cli_wrapper.py` defines `ComposeCLI`; `models.py` holds configuration, project, and event models.
Build progress overrides and Compose globals follow [the package guide](../../AGENTS.md).
`run`/`execute` `envs` and `build` `build_args` follow its [shared environment transport](../../AGENTS.md#container-environment-transport), including the interpolation caveat.

## Configuration Models

- Compose models inherit ordinary Pydantic `BaseModel`, not `DockerCamelModel`.
- `config()` returns `ComposeConfig`; `config(return_json=True)` returns
  `dict[str, JsonValue]`, preserving fields that aren't declared in the typed model.
- Keep nested JSON fields typed as `JsonValue`, including scalar/list values where allowed.
- Output enums retain `EnumType | str | None` so unfamiliar wire strings survive parsing.
- Preserve `Field(default_factory=...)` declarations and independent mutable defaults.

## Service Selection

- `None` selects all services and `[]` skips execution for `build`, `create`, `down`,
  `kill`, `pause`, `pull`, `push`, `restart`, `rm`, `start`, `stop`, `unpause`, `up`, and `watch`.
- `top([])` returns an empty `ComposeTopResult`, or `""` with `raw=True`, without CLI lookup.
- Don't generalize empty-selection behavior: `ps([])`, `logs([])`, and `events([])` select all.

## Execution And Returns

- `build`, `down`, `pull`, and `up` reject `quiet=True` with `stream_logs=True`.
- `build(print=True)` still returns `None` when blocking, or an output stream with `stream_logs=True`.
- Streamed `create()` prints its command only with `raw_diagnostics=True`; returned log records are unchanged.
- Record-stream branches of `build`, `create`, `down`, `logs`, `pull`, `run`, `start`, `stop`, and `up` expose owned `ProcessStream`, not generic iterables; records are `(source, bytes)`.
- `run()` otherwise returns attached `str` or detached `Container` retaining its config and complete captured ID as immutable identity, without truncation or a second lookup. Blocking and `[]` no-op `None` arms stay unchanged; guard optional streams before entering their context.
- `run()` defaults to `tty=True`. Reject TTY plus streaming, detach plus streaming,
  and detach plus TTY; callers must set `tty=False` for detached or streamed runs.
- `ps()` returns container resources. Reject `ps()` and detached `run()` in dry-run mode
  before execution; attached `run(tty=False)` can return preview text instead.
- Non-detached `run()` (captured, TTY, or streamed) and `execute()` (non-streamed; captured or TTY) explicitly pass `workload_status=True`. With originating-client `strict_workload_errors=True`, nonzero exits raise exact `DockerException`; detached and other Compose launches retain specialization. `execute()` has no stream mode; don't add one for this policy. See [shared classification rules](../../AGENTS.md#configuration-and-policy).

## Local Deadlines

- `build`, `execute`, `run`, `pull`, `push`, `watch`, `logs`, and `events` take keyword-only `timeout`: positive finite seconds from the
  local CLI launch, or `None` to inherit `ClientConfig.default_timeout`. Expiry kills and reaps only the local CLI and
  raises `DockerTimeout`; it doesn't stop services, undo pulled or pushed layers, or change post-exit pipe draining.
  A returned stream's deadline advances while nothing consumes it.
- `build()`, `pull()`, `push()`, and `watch()` validate `timeout` before the `[]` no-op: an invalid value raises `ValueError`
  for `[]` and `None` selections alike, and a valid one with `[]` returns `None` without a launch. `pull()` validates
  before its quiet/stream conflict check. Blocking `push()` returns `None`; `pull(stream_logs=True)` returns a stream.
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

## Regression References

Paths below are relative to `tests/pywhales/`; collection/runtime cautions are in [the test guide](../../../tests/AGENTS.md).
- `test_compose_modernization.py`: argv, per-build progress, empty selection, dry-run resources, watch ownership.
- `test_compose_build_enum_json.py`: nested JSON, enum/string outputs, independent defaults, raw config.
- `test_exceptions.py`: strict-workload marks on non-detached `run`/`execute`, with detached classification controls; shared captured and stream exception paths are covered in `test_utils.py` too.
- `components/test_compose.py`: top layouts/raw/no-op/error cases, logs/events ownership, legacy integration.
- `components/test_command_deadlines.py` and `components/test_deadline_forwarding.py`: run/log/event/pull/push/watch
  deadline execution, forwarding, and empty-selection validation.
- Static contracts: `test_compose_type_contracts.py`, `test_type_contracts.py` (`_check_owned_stream_contracts`),
  `test_command_deadline_types.py`, and `test_enum_json_type_contracts.py`.
