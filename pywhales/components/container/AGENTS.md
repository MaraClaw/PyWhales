# Container Component

## Navigation

- `cli_wrapper.py`: `Container`, `ContainerCLI`, launch/execute overloads, and `ContainerStats`.
- `models.py`: inspect snapshots, including state, host config, mounts, and network settings.
- `top.py`: Docker `ps` option grammar used by `ContainerCLI.top()`.

## Cleanup And Launch

- `Container.__exit__()` reads host config and state, stops only when `running is True`,
  then calls `remove(volumes=True)` unless `auto_remove is True`.
  Tolerate `NoSuchContainer` during inspection, stop, or removal; keep `resource_cleanup()`.
- Both `run()` and `create()` use `ImageCLI(self.client_config)` before launching:
  `pull="missing"` calls `_pull_if_necessary()`; `pull="always"` calls `pull(str(image))`.
  Keep shared option forwarding aligned between their separate argument builders.
- `run()`, `create()`, and `execute()` require an argv sequence for `command`.
  A shell command string raises `TypeError`; don't split it implicitly.
- Their `envs` share the [runtime environment transport](../../AGENTS.md#container-environment-transport); keep that policy out of separate builders.

## Output Modes

- Preserve `run()` overloads: detached calls return `Container`, streamed calls return owned
  `ProcessStream` of `(source, bytes)`, and ordinary foreground calls return `str`.
  `detach=True` with `stream=True` raises `ValueError`.
- `prune(stream_logs=True)` also returns `ProcessStream`; blocking prune remains `None`. Close early or use a context manager; local close cannot undo removal.
- `execute()` returns `None` when detached, `ProcessStream` when streamed, otherwise `str`.
  Both TTY plus streaming and detach plus streaming are invalid combinations.
- `start()` streaming requires `attach=True` and a single container; attaching multiple
  containers is invalid. Unattached calls return `None`; attached, nonstreamed calls return `str`.
- Attached `run()`, non-detached `execute()`, and `start(attach=True)` explicitly pass `workload_status=True` in captured and streamed modes, including handle/root aliases. With `strict_workload_errors=True` on the originating client, nonzero exits raise exact `DockerException`; detached/unattached launches and the image/inspect preflights keep stderr-based specialization. Never mark the preflight or infer workload status from argv; shared exception paths live in [the runtime guide](../../AGENTS.md#configuration-and-policy).
- Logs use the originating client's general `stream_buffer_bytes` at the shared stream boundary,
  including when collecting text. Keep the owned transport closed on collection/decoding failure.
  `follow=True` without `stream=True` can block until the container stops.
  `Container.logs()` still returns `str`.

## Local Deadlines

- `wait`, `run`, `execute`, `start`, `copy`, `export`, and `logs` take keyword-only `timeout` on `ContainerCLI`, the matching
  root aliases (`docker.run`, `docker.start`, and so on), and the `Container` handle (`execute`, `start`, `copy_from`,
  `copy_to`, `export`, `logs`; there's no handle `run()`). `None` inherits `ClientConfig.default_timeout`.
  Expiry kills and reaps only the local CLI and raises `DockerTimeout`; it doesn't stop or cancel container-side
  work. Post-exit draining is unchanged. Parent and runtime guides cover the shared semantics.
- `start()` validates `timeout` first in every mode (unattached, captured, streamed) and entry point, before the
  container iterable is consumed or the empty-request return. An invalid value raises `ValueError` even for `[]`;
  a valid one with `[]` returns `None` without a launch. The multi-container attach and unattached-stream errors
  come after it. The handle forwards positional `attach`, `interactive`, `stream`, and `detach_keys` unchanged.
- Preflights stay outside the selected per-launch deadline. `run()` completes `ImageCLI` lookup or pulling and
  `logs()` completes inspection first; `execute()`, `copy()`, and `export()` finish validation, argv assembly, and
  executable lookup first. A detached `run()` doesn't budget the returned handle's lookup.
- Attach and stream ownership is unchanged. Streamed `start()`, `execute()`, `run()`, and `logs()` return owned
  streams whose deadline advances even when nobody iterates; close them or use the context manager on early exit.
  TTY output is forwarded and the returned text is empty. `copy()` local paths are on the Python host.
- `wait()` validates before consuming iterable input or returning `[]`; scalar returns stay `int` and iterable
  returns stay `list[int]`. The root alias forwards the same keyword-only deadline.
- `prune()` keeps positional `filters`/`stream_logs` and adds keyword-only `timeout` on every overload. Validate before consuming filters or lookup; blocking returns `None`, streaming returns eager owned `ProcessStream`. Its launch-relative deadline advances while unconsumed, retains partial output, and delivers timeout once after local cleanup. Early close is idempotent; timeout or close makes no claim that daemon-side pruning stopped or rolled back.

## Batch Inspection And Top

- Batch `inspect()` uses one command and preserves request order and duplicates.
  Validate response count, model data, and reference identity before caching snapshots:
  accept matching ID prefixes or names with an optional leading slash.
  An empty batch returns `[]` without resolving the executable.
- `top()` returns `ContainerTopResult` by default; `raw=True` returns CLI text.
  Docker `ps` tokens follow the container argument; don't apply the Compose grammar.
  Parsed mode supports explicit `-o`/`--format` fields, including heading aliases.
  Unsupported fields, headerless output, and ambiguous implicit-column selections require
  `raw=True`; keep free-text command fields last when requesting parsed output.

## Matching Tests

Paths below are relative to the repository root.
- `tests/pywhales/components/test_container.py`: mocked top, batch inspection, cleanup races,
  eager-log decoding cleanup, and streaming-log ownership; also contains live component tests.
- `tests/pywhales/test_container_image_enum_json.py`: mocked filter argv and enum/JSON snapshots.
- `tests/pywhales/test_exceptions.py`, `tests/pywhales/test_utils.py`, and `tests/pywhales/test_client_config.py`: local-child strict-workload classification, both stream failure paths, explicit wrapper marks, alias/handle forwarding, and default/detached/preflight controls.
- `tests/pywhales/components/test_command_deadlines.py`: launch, log, copy/export, and start validation,
  partial output, reaping, and output shapes.
- `tests/pywhales/components/test_deadline_forwarding.py`: run/execute/log budgets, `start` in every mode and entry,
  empty and invalid start requests, handle/root aliases, and unbudgeted preflights.
- `tests/pywhales/test_command_deadline_types.py`: static run/execute/log/start/copy/export overloads,
  handle forwarding, and aliases.
- `tests/pywhales/test_type_contracts.py`: inspect/top and owned prune/run stream contracts; environment branches: `tests/pywhales/components/test_env_forwarding.py` and `test_env_branch_transport.py`.
