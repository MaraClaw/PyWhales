# Containers, Output, and Lifecycle

## Translate a Foreground Run

```python
from pywhales import docker

output = docker.run(
    "alpine:3.22",
    ["printenv", "GREETING"],
    envs={"GREETING": "hello"},
    remove=True,
)
print(output)
```

This captures stdout as `str`, inherits stderr, and removes the disposable
container when it exits. Commands must be sequences, not `"printenv GREETING"`.
Use string values in `envs`.

| CLI option | Python shape |
| --- | --- |
| `-e KEY=value` | `envs={"KEY": "value"}` |
| `-p 8080:80` | `publish=[(8080, 80)]`, host port first |
| `-p 127.0.0.1:8080:80` | `publish=[("127.0.0.1:8080", 80)]` |
| `-v source:/work:ro` | `volumes=[(source, "/work", "ro")]` |
| `--rm` | `remove=True` |
| `-d` | `detach=True` |

Use an absolute host path for a bind mount; `pathlib.Path` is accepted. For
example, with `source = Path.cwd().resolve()`, mount that directory read-only
and run `["ls", "/work"]`. Import `Path` from `pathlib`. A remote engine must
have the source path on its own host.

## Startup Health Interval And Approved Profiles

Container `create` and every `run` mode, including `docker.create`/`docker.run`
aliases, accept `health_start_interval=None`. A supplied value must be positive
whole seconds: an exact `int` (not a bool or float), or an integral-second
`datetime.timedelta`. Invalid values raise `ValueError` before image preflight
or CLI lookup. It forwards `--health-start-interval <seconds>s`; omission leaves
the image setting unchanged. This is the check interval during the startup
period, not `health_start_period`, `health_interval`, or a convergence timeout.
The operator must qualify the selected CLI and engine for Docker API 1.44+.
PyWhales doesn't probe versions, retry without the flag, or silently downgrade.

```python
from pywhales import docker

# Run only on an authorized, operator-qualified API 1.44+ target.
with docker.run(
    "alpine:3.22",
    ["sleep", "30"],
    detach=True,
    health_cmd="true",
    health_start_period=20,
    health_start_interval=2,
    stop_timeout=1,
) as container:
    print(container.id)
```

For application-approved creation, import `CreateRequest`, `SecurityProfile`,
`RuntimePolicy`, `plan_create`, and `create_from_plan` from `pywhales.profiles`,
not the package root. Direct calls don't apply profiles automatically.
`CreateRequest.health_start_interval` accepts only `None` or a positive exact
integer in seconds, not a timedelta. The profile's
`max_health_start_interval=None` means no approval, not an unlimited interval;
an explicit positive exact integer ceiling is required for a supplied request.
Above-ceiling requests fail with `PolicyReason.LIMIT`; missing approval uses
`NOT_APPROVED`. Invalid request or ceiling types use `INVALID`.

The selected `RuntimePolicy.supported_controls` must include the baseline
`CONTROLS` and, only when an interval is requested, `"health_start_interval"`.
Declare that extra control only after qualifying this runtime; don't mutate
`CONTROLS` or add it to every runtime by assumption. Missing runtime support is
`UNSUPPORTED`. Omitted intervals need no new runtime control or profile ceiling.

Given the application's existing `client`, `profile`, and `request`, and explicit
operator approval of the selected runtime and a 5-second ceiling:

```python
from dataclasses import replace

from pywhales.profiles import CONTROLS, RuntimePolicy, plan_create

qualified_runtime = RuntimePolicy(
    name=request.runtime,
    rootless=request.rootless,
    supported_controls=CONTROLS | {"health_start_interval"},
)
approved_profile = replace(
    profile, max_health_start_interval=5, runtimes=(qualified_runtime,)
)
requested = replace(request, health_start_interval=2)
plan = plan_create(approved_profile, requested, client)  # no Docker I/O
```

`plan_create` performs pure validation. `create_from_plan(client, plan)` rechecks
the plan and endpoint binding before preparation, pull, inspect, or creation,
then forwards the approved interval to `client.container.create`. It creates
but doesn't start the container; the application owns its later lifecycle.

## Container Environment Values

`envs={...}` sets values inside the container. In container `create`, `run`, and
`execute`, service `create`, and Compose `run` and `execute`, eligible ordinary
names use `--env KEY`: PyWhales puts the value in that launch's child environment
for the CLI to forward. Empty values remain empty strings. This doesn't change
the parent Python environment or use temporary env files.

By default these entries stay literal `KEY=VALUE` arguments, in their original
order, instead of being rejected:

- CLI-control names, matched case-insensitively: the prefixes `DOCKER_`,
  `COMPOSE_`, `BUILDX_`, `BUILDKIT_`, `XDG_`, `SSH_`, `GIT_`, `OTEL_`, `LD_`,
  `DYLD_`, `LC_`, `NODE_`, and `PYTHON`, plus names such as `PATH`, `HOME`,
  `TMPDIR`, `TERM`, `LANG`, `TZ`, and proxy variables.
- Invalid entries: an empty name, a name containing `=` or NUL, a value
  containing NUL, or a non-string key.
- Duplicate names after stringification, including names that differ only by
  case on Windows.
- Names colliding with the effective child environment or an active
  frozen environment snapshot, compared case-insensitively on Windows.

With `inherit_environment=False`, ordinary names present only in excluded
ambient state don't collide; explicit client-overlay entries still do. Reserved
names remain reserved. See [CLI environment isolation](client-and-errors.md#isolate-the-cli-environment).

`DockerClient(strict_env_transport=True)` turns every entry above into a
`ValueError` with a fixed message that discloses no name or value; one ineligible
entry fails the whole call. See
[Strict environment transport](client-and-errors.md#strict-environment-transport).
The same check covers container `create`/`run`/`execute`, service `create`,
Compose `run`/`execute`, and Buildx or Compose build args. For container `run`
and `create`, the `pull="missing"` or `"always"` image step runs before `envs`
are evaluated, so a strict rejection can come after an image pull.

This reduces eligible values in argv; it doesn't promise that secrets never
appear there, and a child environment isn't a secret vault. Use the application's
secret policy. `DockerClient(environment=...)` configures the CLI environment,
not container values; keep it distinct from `envs`. For Compose, forwarded
values also enter the Compose CLI's environment and can affect `${NAME}`
interpolation elsewhere in its configuration.

## Choose the Result Type

| Call mode | Result |
| --- | --- |
| `docker.run(...)` | `str` containing captured stdout |
| `docker.run(..., detach=True)` | `Container` |
| `docker.run(..., stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.execute(container, [...])` | `str` containing stdout |
| `docker.execute(container, [...], detach=True)` | `None` |
| `docker.execute(container, [...], stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.logs(container)` | `str` assembled from stdout/stderr log chunks |
| `docker.logs(container, stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.start(container)` | `None` |
| `docker.start(container, attach=True)` | `str` containing captured stdout |
| `docker.start(container, attach=True, stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.ps(all=True)` | `list[Container]`, including stopped containers |
| `docker.wait(container)` | `int` exit code |
| `docker.wait([container, ...])` | `list[int]` in Docker's output order; `[]` for an empty iterable, with no launch |

Streaming `container.run`, `container.prune(stream_logs=True)`, and
`service.logs(stream=True)` are also typed as closeable `ProcessStream` returns.
Container prune is destructive and suppresses confirmation; stream cleanup
doesn't undo deletions, so require explicit authorization before calling it.

For stream tuples, `source` is `"stdout"` or `"stderr"`. Don't decode the tuple;
decode its byte payload. Don't combine `detach=True` with streaming. For
`execute`, `tty=True` is also incompatible with streaming. Leave TTY allocation
off for captured automation; use `interactive=True, tty=True` for an actual
interactive terminal session. `start(..., stream=True)` requires `attach=True`,
and attaching more than one container raises `ValueError`; an empty iterable
returns `None` without a launch. `docker.start`, `docker.container.start`, and
`Container.start()` share these rules and their return shapes.

For workloads whose stderr may resemble Docker errors, configure
`DockerClient(strict_workload_errors=True)`. Attached `run`, non-detached
`execute`, and `start(attach=True)` then raise exact `DockerException` on nonzero
exits, in captured/TTY and supported streamed modes, including handle and root
aliases. Detached/unattached calls and image/inspect preflights keep specialized
errors. See [Workload failures](client-and-errors.md#separate-workload-failures-from-docker-errors)
for defaults, preserved error attributes, and timeout/cancellation behavior.

`run`, `execute`, `logs`, `start`, and `wait` each accept a keyword-only `timeout` in
positive finite seconds, replacing the client's `default_timeout` for that one
launch; omitting it (`None`) inherits the client default, which is itself
`None` (unbounded) unless set on `DockerClient(...)`. Expiry kills and reaps the
local CLI and raises `DockerTimeout` with retained partial output, not a replay
of streamed stdout already delivered to the consumer.
It is not a daemon-side container timeout: it does not stop a started or
detached container or guarantee cancellation of the command inside it. See
[Client, setup, and errors](client-and-errors.md) for the default-deadline
contract. Image pull/inspect preflights and detached-result inspection are
outside the selected run/execute/logs launch budget; a configured client
default still applies separately to those launches. `logs` inspects the
container before its logs CLI starts, outside the deadline. Invalid `timeout`
values are rejected before that inspection; with a valid timeout, a missing
container raises `NoSuchContainer` before log collection.

`wait` (`docker.wait(x, *, timeout=None)`, also `docker.container.wait`) blocks
until the selected containers stop, so give anything that may keep running a
deadline. It validates `timeout` before consuming the input: an invalid value
raises `ValueError` even for `[]`, while a valid one with `[]` returns `[]`
without a launch. A single reference returns an `int`, an iterable of references
a `list[int]`. Expiry raises `DockerTimeout` without stopping the containers.

For running/healthy readiness plus designated successful one-shot completion,
use `capture_containers()` and `wait()` from `pywhales.convergence`, not
`docker.wait()`. They return typed observation outcomes instead of exit codes;
set `fail_on_exit=True` at capture to fail when a long-running member exits
without restarting, even with exit code 0, or `fail_on_unhealthy=True` to fail on
an eligible running member reporting `unhealthy`. Both default to `False`. See
[Standalone containers](observation-and-convergence.md#standalone-containers)
for restart-budget precedence and designated completion rules.

`start` takes `timeout` in every mode (unattached, attached and captured, or
streamed) and through all three entry points, validated before the container
iterable is consumed. An invalid value raises `ValueError` even for `[]`, while
a valid one with `[]` returns `None` without a launch. A streamed `start`,
`run`, `execute`, or `logs` deadline keeps advancing while nobody iterates, so
exhaust the stream or close it on early exit; closing reaps only the local CLI.

## Bound Prune CLI Launches

Container, image, network, volume, and system `prune()` accept keyword-only
`timeout=None`. Give each cleanup call its own positive finite seconds;
`None` inherits the client's launch-snapshotted default, and an explicit value
replaces it without changing shared configuration. Validation rejects booleans,
zero, negative and nonfinite values before filters are consumed or the executable
is resolved. This is a local CLI deadline, not an object-age `until` filter or a
Docker timeout flag.

| Call | Result |
| --- | --- |
| `client.container.prune(..., timeout=10)` | `None` |
| `client.container.prune(..., stream_logs=True, timeout=10)` | Owned `ProcessStream` of `(source, bytes)` |
| `client.image.prune(..., timeout=10)` | `str` CLI output |
| `client.network.prune(..., timeout=10)` | `None` |
| `client.volume.prune(..., timeout=10)` | `None` |
| `client.system.prune(..., timeout=10)` | `None` |

Use the explicit command-group methods, not an assumed root `prune` alias.
Keep each method's flags and filter grammar: system accepts a mapping or pairs,
while the other four require iterable pairs and reject mappings. Repeated pairs
retain order. Check installed signatures before selecting filters or options.

Container streaming starts eagerly and its deadline advances even before the
first iteration. Exhaust it or use a context manager; early close is idempotent,
and a recorded timeout is delivered once after local child/pipe/thread cleanup.
Retained partial output does not replay streamed stdout already delivered.
Every prune suppresses confirmation and deletes resources: run only the cleanup
the user authorized. A timeout kills and reaps the local CLI, but doesn't prove
daemon-side removal stopped or rolled back. Do not automatically retry a prune
after an ambiguous timeout.

## Bound Stream Buffering

Set `DockerClient(stream_buffer_bytes=1_048_576)` to bound buffered output at
shared process-stream boundaries, including logs collected into text. The
default `None` is unbounded; a bound must be a positive integer, not a boolean.
This is a client option, not a `run(..., buffer_bytes=...)` argument.

The stdout/stderr payload queue shares one budget. Each unfinished pipe record
and retained stderr history has a separate bound, so this isn't a total-memory
cap. Queue pressure blocks producers rather than dropping output; a record
(including its newline) or stderr history exceeding its bound raises
`BufferError`. For pull/push progress, the independent
`image_stream_buffer_bytes` override falls back to this general budget when
`None`; see [Registry transfers and archives](registry-and-archives.md).

## Own Detached Containers and Streams Separately

```python
from pywhales import docker

with docker.run("alpine:3.22", ["sleep", "300"], detach=True) as container:
    print(docker.execute(container, ["echo", "ready"]))
    with docker.logs(container, tail=20, stream=True) as logs:
        for source, chunk in logs:
            print(f"{source}: {chunk.decode(errors='replace')}", end="")
```

The outer context owns a newly created disposable container. On exit it stops
the container if still running, then removes it with `volumes=True` unless
Docker auto-removal applies. Don't use this context pattern around an existing
container returned by inspect or list: exiting would remove someone else's
resource. Keep persistent data out of this disposable pattern.

The inner context owns only the local CLI process, pipes, and reader threads.
Consume the stream to exhaustion to observe a nonzero command exit as
`DockerException`. Exiting early or calling `close()` cancels and reaps the
local CLI; that cancellation isn't proof the command succeeded and doesn't
remove daemon-managed containers. A bare loop `break` doesn't close a stream.
Recorded deadline, observation, or reader errors remain deliverable once after
`close()` or on a clean context exit; cleanup preserves an active body exception.

`follow=True` can keep log collection open indefinitely. Prefer a snapshot or
bounded `tail` unless following is requested; if following, provide a deliberate
cancellation boundary and keep the stream in a context manager.

Container properties such as `name` and `state` can issue inspect calls and use
a short-lived cache. Read a property once into a local variable for a coherent
decision; use `container.reload()` when an explicit refresh is required.
A reload must still describe the handle's immutable ID; a mismatched snapshot
raises `ParsingError` and leaves the cached data and refresh time untouched.

## Copy Files or Export a Filesystem

`docker.copy(source, destination, *, timeout=None)` accepts a local path and a
`(container, path_in_container)` endpoint, in either direction. Local paths refer
to the Python host, unlike bind mounts. It returns `None` and doesn't accept
byte iterators. `Container.copy_from()` and `copy_to()` forward the same
keyword-only local deadline.

`docker.export(container, output, *, timeout=None)` writes the container
filesystem to a local tar path and returns `None`; `Container.export()` takes
the output path and the same deadline. It doesn't return archive chunks and
isn't an image backup. For image archives, use
[save and load](registry-and-archives.md#save-and-load).
