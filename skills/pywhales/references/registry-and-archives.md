# Registry Transfers and Archives

`docker.pull`, `docker.push`, `docker.save`, and `docker.load` are aliases of
`docker.image.pull`, `docker.image.push`, `docker.image.save`, and
`docker.image.load`. They move images between the daemon, a registry, and
local tar archives.

## Pull And Push

```python
from pywhales import docker

image = docker.pull("alpine:3.22")
images = docker.pull(["alpine:3.22", "busybox:latest"])
docker.push("example.registry/app:release")
```

Iterable references are deduplicated in first-seen order before transfer.
Blocking calls run eagerly: one reference pulls or pushes during the call;
several unique references use up to four concurrent workers. `pull([])`
returns `[]` without starting a CLI. `push([])` returns `None`, even with
streaming; this is a deliberate asymmetry between the two methods, not a bug
to normalize away.

`pull([], stream_logs=True)` instead returns an empty context-managed, closeable
stream without executable lookup or a CLI launch. Its `close()` is idempotent;
it no longer needs a `contextlib.closing` wrapper. This doesn't change blocking
`pull([]) == []` or `push([]) is None`, even with push streaming enabled.

```python
with docker.pull([], stream_logs=True) as progress:
    assert list(progress) == []
progress.close()  # safe after context exit too
```

`push` batch-inspects every deduplicated reference before starting any
transfer, including streamed ones, so a missing local image raises
`NoSuchImage` before a registry connection is attempted.

| Call | Blocking result | `stream_logs=True` result |
| --- | --- | --- |
| `docker.pull("x")` | `Image` | Owned `(image_reference, bytes)` stream |
| `docker.pull(["x", "y"])` | `list[Image]`, even for one unique reference | Owned stream across up to four workers |
| `docker.push("x")` | `None` | Owned `(image_reference, bytes)` stream |
| `docker.push(["x", "y"])` | `None` | Owned stream, or `None` for `[]` |

Streaming starts eagerly for one unique reference; for several, workers start
on first consumption of the stream, not at construction or context entry, and
at most four concurrent transfers run at once. A bare loop `break` doesn't
close the stream; exhaust it, use its context manager, or call `close()`.
For a multi-reference stream, the first worker's error stops sibling and
queued transfers without waiting for further consumption; that error is
delivered once through iteration, including after an explicit `close()`.
Closing cancels only local CLI processes, not registry pushes or daemon-side
transfers already in progress.

`pull` and `push` both accept a keyword-only `timeout` in positive finite
seconds, timed from each transfer subprocess's own launch and replacing
`default_timeout` for that call. `None` inherits the client default rather than
disabling it. Both validate it before anything else, so a boolean, zero,
negative, or nonfinite value raises `ValueError` before references are consumed
or inspected, even for `[]`; a valid value with `[]` keeps the empty results
above. Every blocking pool task gets a fresh deadline, and so does every lazy or
queued stream child once its worker launches it, never measured from queueing
or stream creation. Once a child CLI has launched, its deadline keeps advancing
while nobody iterates. `quiet=True` can't be combined with `stream_logs=True`
on either call.

`push`'s batch inspection, any image inspection around a pull, and executor
queueing sit outside the explicit budget; a configured client default still
governs those inspections as their own launches. Expiry kills and reaps only the
local transfer CLI and raises `DockerTimeout` with retained partial output, not
a replay of progress already delivered. It doesn't undo a registry or daemon-side
change. A child's
`DockerTimeout` in a multi-reference stream is that stream's first worker error,
so the delivery rules above apply and the stream stays closeable.

Inside `observation()`, blocking pull/push calls need `quiet=True` so both output
pipes are captured; `stream_logs=True` instead works with `quiet=False`. Pool
tasks and stream workers inherit the creating observation, including when a
multi-image stream is consumed after the scope exits. See
[Observation budgets and convergence](observation-and-convergence.md).

`docker.manifest.push(..., timeout=30)` and
`docker.plugin.push(..., timeout=30)` also accept keyword-only local CLI
deadlines; a `Plugin` resource's `push(timeout=30)` forwards the same override.
Manifest preflight inspection is outside the selected push budget. These
blocking operations return `None`. Plugin `enable(timeout=...)` is a different,
daemon-side option, not this per-launch deadline.

`image_stream_buffer_bytes` on `DockerClient`/`ClientConfig` is an
independent override for pull/push progress-stream budgets; when it's `None`
(the default), the general `stream_buffer_bytes` applies instead. This progress
budget is selected when the public pull/push operation starts. Direct archive
bytes aren't progress records and aren't limited by these budgets; path-based
`load` still uses the general budget for its progress output.

## Save And Load

```python
from contextlib import closing

from pywhales import docker

docker.save("alpine:3.22", output="alpine.tar")
docker.load("alpine.tar")

with closing(docker.save("alpine:3.22")) as chunks:
    with open("alpine-streamed.tar", "wb") as output:
        for chunk in chunks:
            output.write(chunk)
```

`save(images, output=None, *, timeout=None)` validates the deadline and rejects
empty input, then inspects every requested image eagerly, raising
`NoSuchImage` for a missing one before starting the save CLI. An
explicit `output` path blocks and returns `None`. Omitted `output` returns a
lazy generator of raw archive-byte chunks, not `(source, bytes)` progress
records: its save subprocess starts on first iteration, not on the call
itself. Exhaust or explicitly close that generator to leave its subprocess
context; if it's discarded unconsumed, no save subprocess or archive-save
telemetry starts. The eager inspection can still emit its own telemetry.
Closing a started generator kills a still-running local save CLI, reaps it, and
joins its accounting thread. It doesn't undo daemon-side work.

`load(input, quiet=False, *, timeout=None)` accepts a path, complete bytes, or
an iterator of byte chunks. Path input runs an eager, process-backed progress stream,
printing decoded lines unless `quiet=True`. Byte or iterator input is written
to a local CLI's own stdin and fully consumed during the call; these archive
bytes aren't progress-log records, so the selected deadline still reaps the
local CLI even if the input iterator stalls, though the iterator's own Python
code can't be interrupted. Both forms return `list[str]` of the image tags/IDs
parsed from `Loaded image` output lines, in output order.

`save` and `load` also accept a keyword-only `timeout`, replacing the client
default for that call (`None` inherits it) and validated before images are
inspected or input is read. `save` gives its eager inspection and its save CLI
separate deadlines; for the lazy generator the save clock starts at first
iteration, using the default as configured at that moment when `timeout` is
omitted. Expiry kills and reaps the local CLI only.

## Bound Archive Uploads

To cap bytes sent by byte/iterator `load()`, set
`InspectionPolicy(max_total_stdin_bytes=...)` inside `observation()`, alongside
the three required per-command limits. This is an aggregate scope budget, not
a `load()` keyword or a progress-buffer setting:

```python
from pywhales import InspectionPolicy, docker, observation

policy = InspectionPolicy(
    max_references=1,
    max_argv_bytes=1_048_576,
    max_response_bytes=1_048_576,
    max_total_stdin_bytes=64 * 1024 * 1024,
)
with open("alpine.tar", "rb") as archive, observation(policy):
    tags = docker.load(iter(lambda: archive.read(65_536), b""), timeout=60)
```

Only bytes actually accepted by local stdin-pipe writes count, including partial
writes; every enclosing stdin account is charged. Exact-budget EOF succeeds.
The optional stdin total must be a positive integer, never a boolean; `None`
leaves archive uploads unbounded unless an enclosing scope supplies a limit.
Further nonempty input raises `ObservationLimitError` with `reason="total_stdin"`
after local CLI cleanup, without sending bytes beyond the budget. Uploads retain
their launch-time accounts after scope exit. Output is accounted separately:
direct archive reads use `max_total_stream_bytes`, not the captured-response
total. Path-based `load("alpine.tar")` lets the CLI read the file itself, so it
doesn't charge an upload stdin budget.

Use cooperative chunk producers: deadlines and cancellation can reap the local
CLI but cannot interrupt arbitrary Python code inside the input iterator. For
nested limits and observation exceptions, see
[Observation budgets and convergence](observation-and-convergence.md).

## Telemetry Families

With `telemetry_observer` set, the byte-based `save()` generator reports under
`archive_save`, and the byte/iterator `load()` path under `archive_load`.
Path-based `--input`/`--output` transfers and ordinary
pull/push progress streams report under the regular `captured`/`stream`
families instead. See
[Telemetry and admission](telemetry-and-admission.md) for the full schema.

Schema-8 observation preflight refusals use those same families, emit no
launch/completion pair, and preserve the original exception without starting a
child or reading upload input. A lazy save checks and can emit its refusal at
first iteration, not construction; eager inspection is a separate captured
operation. Earlier argument validation is not a refusal event.
`InspectionPolicy.max_total_launch_attempts` also bounds archive launch
reservations across enclosing shared observation accounts. A lazy save reserves
at first iteration, separately from its eager inspect preflight. Exhaustion
raises `ObservationLimitError` with reason `total_launch_attempts` before child
creation or upload input reads; accepted reservations are not refunded after
later failure. See [Observation budgets](observation-and-convergence.md#bounding-batch-inspection).
`max_concurrent_children` separately limits owned local children until reap.
Saturation consumes no launch attempt; failed spawn returns its child slot but
not its accepted attempt. Lazy save charges its eager inspection separately and
reserves the save slot only on first iteration. Reaped output may remain buffered
after that slot is released; exhaust or close the generator to finish cleanup.

## Other Operations

`docker.tag(...)` and `docker.images()` (`image.list()`) cover local tagging
and discovery. `image.remove(...)` and `image.prune(...)` delete images;
`prune` suppresses Docker's confirmation prompt, so require explicit deletion
authorization. An `Image` context manager removes the image on exit: never use
it as automatic cleanup for a borrowed image. `Image.copy_from()`/`Image.copy_to()` use a
temporary container, removed on exit, to move files between the local
filesystem and an image; both forward `pull`, defaulting to `"missing"`, to
container creation. `docker.import_(source, tag=...)` imports a tarball's
contents as a new filesystem image; it is unrelated to `load`, which restores
images previously produced by `save`/`docker save`.

`image.prune(..., timeout=10)` returns CLI text and accepts a keyword-only local
deadline, validated before filters or lookup. `None` inherits the client default;
expiry doesn't undo daemon-side deletion and must not trigger an automatic retry.
For all five prune result/filter contracts, see
[Bound prune CLI launches](containers-and-output.md#bound-prune-cli-launches).

Image `copy_from` and `copy_to` also accept keyword-only `timeout=None`: an
explicit value gives each helper launch its own deadline, including inspection,
pull, temporary-container creation, copy, commit when needed, and cleanup. It
isn't one total operation budget. `copy_from` returns `None`; `copy_to` commits
and returns a new `Image`, leaving the original image unchanged.
