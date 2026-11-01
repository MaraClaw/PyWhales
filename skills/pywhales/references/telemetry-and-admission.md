# Telemetry And Admission

Both features are opt-in. Neither name is exported from the `pywhales`
package root; import them from `pywhales.telemetry` and `pywhales.admission`.
Without them, ordinary `DockerClient` calls are unchanged.

## Sanitized Execution Telemetry

```python
from pywhales import DockerClient
from pywhales.telemetry import CompletionRecord, TelemetryRecord


def observe(record: TelemetryRecord) -> None:
    if isinstance(record, CompletionRecord):
        print(record.family, record.outcome, record.duration_seconds)


client = DockerClient(telemetry_observer=observe)
```

Set `telemetry_observer` on `DockerClient` or `ClientConfig` to receive
immutable `LaunchRecord` and `CompletionRecord` objects from the shared captured,
streaming, and direct archive execution paths. This isn't a guarantee that every
possible subprocess path is audited. The observer is snapshotted by reference at each
launch, alongside the rest of the execution policy; later config mutations
affect only new launches, not a stream already in progress. There's no
monitoring dependency, global observer, background worker, or network export
by default.

The vocabulary is closed and currently at `schema_version=4`. `family` is
`"captured"`, `"stream"`, `"archive_load"`, or `"archive_save"`, describing
the shared execution path, never something inferred from argv; see
[Registry transfers and archives](registry-and-archives.md) for exactly
which image operations use the archive families. `outcome` on
`CompletionRecord` is `"success"`, `"failure"`, `"timeout"`, `"closed"`,
`"cancelled"` (an `ObservationCancelled`), or `"limit"` (an
`ObservationLimitError` after launch, including a `max_total_stream_bytes`
overflow, a `max_total_stdin_bytes` refusal, or a `native_argv` refusal).
`ObservationTimeout` and `DockerTimeout`
stay `"timeout"` and `KeyboardInterrupt` stays `"failure"`; a rejection before
launch emits no record. A stream reports the outcome of the error it delivers
(deadline, then scope stop, then reader error), and `close()` keeps a stop
recorded before it, such as a set cancel token; with none it reports
`"closed"`. Each launch attempt has one opaque random `attempt_id`, shared with
at most one completion.

Records never include commands, arguments, endpoint addresses, environment
keys or values, stdin/stdout/stderr payloads, or exception text, even with
`raw_diagnostics=True`. `CompletionRecord` adds `duration_seconds` (monotonic,
including stream consumer pauses until exhaustion/failure/close),
`stdout_bytes`/`stderr_bytes`/`stdin_bytes` (raw byte counts, `None` when
unobserved, never a fabricated zero), and `return_code` (the reaped local
process status, `None` on spawn failure). None of this is admission queue
wait, Docker daemon task duration, or application readiness. A callback's own
printed/logged output is the application's responsibility, not sanitized by
this contract.

`stdin_bytes` counts bytes actually accepted by successful writes to the local
CLI's stdin pipe, including partial acceptance before a failure. It doesn't count
bytes merely yielded by an input iterator and doesn't prove consumption by the
CLI or Docker daemon. An observed empty pipe is `0`; an unobserved channel is
`None`. The same accepted-write measurement charges nested observation stdin
budgets; reaching the exact budget at EOF succeeds, while further nonempty input
reports `"limit"` without writing past it. See
[Bound archive uploads](registry-and-archives.md#bound-archive-uploads).

A stream completes only when its consumer exhausts it, observes a terminal
error, or closes it; a bare loop `break` doesn't promise a completion record.
Observers run synchronously on the launching/consuming/closing thread, must
be quick, thread-safe, and non-reentrant, and must not mutate execution state. An ordinary
`Exception` raised by the observer is contained without being logged and
can't replace a command result.

```python
from pywhales import DockerClient
from pywhales.telemetry import BoundedExporter, TelemetryRecord


def sink(record: TelemetryRecord) -> None:
    print(record)


exporter = BoundedExporter(sink, capacity=256, drop_policy="drop_newest")
client = DockerClient(telemetry_observer=exporter)
try:
    client.ps()
    exporter.flush()
finally:
    exporter.shutdown(flush=True)
```

`BoundedExporter` is an optional thread-safe buffer, not a worker or
transport. It requires a positive integer `capacity` and the explicit
`drop_policy="drop_newest"`; when full, the newest record is dropped and
`dropped` increments. `flush()` exports one finite snapshot and blocks on the
sink; `shutdown(flush=True)` exports the final snapshot and stops admitting
new records, while `shutdown(flush=False)` discards pending records as
dropped. A sink must not recursively call `flush()` or `shutdown()`. Close or
exhaust owned streams before shutdown if their completion records still
matter.

`queued` and `export_errors` expose buffer/sink status. Launch and completion
records can be dropped independently: don't assume every exported launch has
an exported completion. The application owns flushing, sink privacy, and shutdown.

## Bounded Endpoint Admission

```python
from pywhales import DockerClient
from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    SaturationPolicy,
)

client = DockerClient()
control = PressureClass("control")
owner = AdmissionOwner(
    EndpointIdentity("primary"),
    {control: ClassLimit(active=2, pending=4, policy=SaturationPolicy.WAIT)},
)

containers = owner.request(control, queue_timeout=5.0).call(client.ps)
```

`pywhales.admission` is an application-owned layer for limiting work routed
through it. It doesn't hook into `DockerClient` automatically; only
operations explicitly wrapped with `owner.request(...)` count toward a
class's limits. Choose pressure classes and `ClassLimit(active=..., pending=...)`
yourself; PyWhales doesn't classify operations for you. Endpoint and class
identifiers are safe ASCII tokens (at most 64 characters); don't put
credentials in them. Use the same `AdmissionOwner` instance for every call
that must share one budget: equal identity strings on separate owner objects
don't combine their counts.

Each reservation carries an application-defined `demand`, defaulting to `1`.
`ClassLimit(..., demand_capacity=50)` optionally caps a class's total active
demand separately from its active-slot count; the default `None` leaves it
unbounded. `owner.request(control, demand=3)` reserves 3 units up front.
When active capacity is unavailable, `SaturationPolicy.WAIT` waits in a
bounded FIFO pending queue (an optional `queue_timeout` bounds that wait
separately from any local CLI deadline); `SaturationPolicy.REJECT` rejects
immediately instead. A full WAIT pending queue rejects new requests; there's
no overflow queue.

`owner.request(...)` returns an inert, single-use handle. Calling `call()` or
`stream()` on it attempts admission and only then runs the supplied callable.
`request.cancel()` can stop a still-pending wait from another thread;
successful cancellation guarantees the callable never starts, but once admitted,
`cancel()` returns `False` and can't undo the operation.

```python
with owner.request(control).stream(
    lambda: client.image.pull(["alpine:3.22", "busybox:latest"], stream_logs=True)
) as records:
    for image, chunk in records:
        print(image, chunk)
```

`stream()` requires a factory returning a context manager yielding an
iterator; the permit stays held until exhaustion, an observed failure, or an
explicit close completes source cleanup and any in-flight read unwinds. A bare
`break`, local child exit, or unobserved source failure doesn't release it.
A plain generator needs wrapping in `contextlib.closing` inside the factory.
Use `stream()`, not `call()`, when returning an owned stream: `call()` releases
its permit as soon as the callable returns. One stream permit covers all direct
children of a composite source, not one permit per transfer worker.
`image.pull([], stream_logs=True)` is already context-managed and closeable,
even though it launches nothing; no wrapper is needed for that result.

An active reservation from `call()` or an open `stream()` can resize its
demand in place with `try_resize(new_total)`, available on both the original
request handle and the returned stream handle, since they share one
reservation. For an active reservation, shrinking or equal demand succeeds;
growth returns `False`, unchanged, if any request is pending in that class or
the aggregate active demand would exceed capacity. Invalid totals (including
booleans, nonintegers, nonpositive values, or a total above class capacity)
raise `ValueError`. Before admission, after release, or once stream cleanup
begins, valid resize attempts raise `RuntimeError` through either handle.
`try_resize` never waits and never releases/reacquires the active slot.

`owner.snapshot()` returns immutable, payload-free endpoint/class snapshots:
active/pending request counts, outcome counts, and request high-water marks;
the separate `demand` snapshot tracks active/pending demand and independent
demand high-water marks. These are local logical reservations, not daemon CPU,
memory, subprocess counts, or a cross-process quota.

`owner.close()` permanently rejects new work and wakes pending callers, but
doesn't cancel active calls or close their streams. After closing owned streams,
`owner.wait_drained(timeout=5.0)` returns whether all active permits drained.
Stream cleanup failures propagate; an independent body and cleanup failure are
preserved together in a `BaseExceptionGroup`.

For sequential work, `owner.iter_calls(control, operations)` consumes an iterable
of callables lazily, admitting one at a time; it isn't a parallel scheduler.
Reentering the same owner from an operation it already admitted fails in the
calling context: treat a composite operation as one admission, not nested
ones.
