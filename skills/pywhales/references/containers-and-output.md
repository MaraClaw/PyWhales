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
see [Standalone containers](observation-and-convergence.md#standalone-containers).

`start` takes `timeout` in every mode (unattached, attached and captured, or
streamed) and through all three entry points, validated before the container
iterable is consumed. An invalid value raises `ValueError` even for `[]`, while
a valid one with `[]` returns `None` without a launch. A streamed `start`,
`run`, `execute`, or `logs` deadline keeps advancing while nobody iterates, so
exhaust the stream or close it on early exit; closing reaps only the local CLI.

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
