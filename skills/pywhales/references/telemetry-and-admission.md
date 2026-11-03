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
immutable `LaunchRecord`, `CompletionRecord`, and `RefusalRecord` objects from the shared captured,
streaming, and direct archive execution paths. This isn't a guarantee that every
possible subprocess path is audited. The observer is snapshotted by reference at each
launch, alongside the rest of the execution policy; later config mutations
affect only new launches, not a stream already in progress. There's no
monitoring dependency, global observer, background worker, or network export
by default.

The vocabulary is closed and currently at `schema_version=8` for launch,
completion, refusal, and admission decision records. On execution records, `family` is
`"captured"`, `"stream"`, `"archive_load"`, or `"archive_save"`, describing
the shared execution path, never something inferred from argv; see
[Registry transfers and archives](registry-and-archives.md) for exactly
which image operations use the archive families. `outcome` on
`CompletionRecord` is `"success"`, `"failure"`, `"timeout"`, `"closed"`,
`"cancelled"` (an `ObservationCancelled`), or `"limit"` (an
`ObservationLimitError` after launch, including a `max_total_stream_bytes`
overflow, a `max_total_stdin_bytes` limit, or a post-start `native_argv` failure).
`ObservationTimeout` and `DockerTimeout`
stay `"timeout"` and `KeyboardInterrupt` stays `"failure"`. A stream reports the outcome of the error it delivers
(deadline, then scope stop, then reader error), and `close()` keeps a stop
recorded before it, such as a set cancel token; with none it reports
`"closed"`. Each launch attempt has one opaque random `attempt_id`, shared with
at most one completion. `LaunchRecord.admission_id` is the innermost observed
admission's opaque ID, or `None` outside one; join completion to launch by
`attempt_id`. Completion records don't repeat the admission ID.

An observation cancellation, deadline or limit exception escaping launch
preflight emits exactly one `RefusalRecord` (`phase="refusal"`), then re-raises
the original exception before any spawn. Its fields are fixed `family`, opaque
`attempt_id`, optional `admission_id`, schema version, phase, and a closed
`outcome` of `"cancelled"`, `"timeout"`, or `"limit"`. There is no matching
launch/completion, duration, byte count or return code. Earlier argument
validation and environment-resolution failures stay outside this contract;
an observation rejected at scope entry also hasn't reached launch preflight.
A depleted `max_total_launch_attempts` allowance produces a `"limit"` refusal
with no launch/completion or upload input read; the refused reservation consumes
no allowance. A saturated `max_concurrent_children` budget likewise refuses with
`"limit"` before spawn, without consuming a launch attempt or child slot. The
records do not include the limit, demand, or rejection reason
text beyond their closed outcome.
A native argv failure after launch telemetry starts instead keeps its launch
and `"limit"` completion.

This covers captured execution, process streams, byte/iterator image load, and
lazy archive save. A lazy save emits its archive refusal only at first iteration
with that launch's snapshotted observer; eager image inspection can independently
emit captured records. Filename-based transfers retain their regular families.
With no execution observer, refusal handling adds no telemetry IDs, clocks or
callbacks. Ordinary callback exceptions cannot replace the refused exception.

Records never include commands, arguments, endpoint addresses, environment
keys or values, endpoint aliases, pressure-class names, callables,
stdin/stdout/stderr payloads, or exception text, even with
`raw_diagnostics=True`. `CompletionRecord` adds `duration_seconds` (monotonic,
including stream consumer pauses until exhaustion/failure/close),
`stdout_bytes`/`stderr_bytes`/`stdin_bytes` (raw byte counts, `None` when
unobserved, never a fabricated zero), and `return_code` (the reaped local
process status, `None` on spawn failure). Execution duration isn't admission queue
wait, Docker daemon task duration, or application readiness; admission decisions
measure their own wait separately below. A callback's own
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

`queued` and `export_errors` expose buffer/sink status. Launch, completion,
refusal, and admission decision records can be dropped independently: don't assume every
exported launch has an exported completion or decision. The application owns
flushing, sink privacy, and shutdown.

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
    owner_pending_demand_capacity=8,
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

`ClassLimit(..., pending_demand_capacity=20)` separately caps the sum of declared
demand waiting in that queue, not active demand or pending request count. It
accepts nonnegative integers excluding booleans; `None` is unbounded. An exact
fit queues, and zero still allows immediate admission when the active slot and
demand limits allow it. When queueing is needed, busy rejection and pending-count
fullness take precedence over the distinct
`AdmissionRejected.reason is RejectionReason.PENDING_DEMAND_FULL` (import both
names from `pywhales.admission`; the enum value is `"pending_demand_full"`). A refused callable or stream
factory never runs. Queue cancellation, timeout, closure and grants remove pending
demand; active resizing does not consume this queue-only allowance. FIFO head
blocking remains unchanged.

`AdmissionOwner(..., owner_pending_demand_capacity=8)` optionally caps the sum of
queued declared demand across all classes on that exact owner. It accepts only
nonnegative exact integers (no booleans); `None` is unbounded. Exact fits queue;
immediate grants bypass it, even at zero. It doesn't cap active demand, active
slots, or resizing. Admission checks closure/reentry and the immediate-grant path
first, then busy rejection, class pending-count fullness, class pending-demand
fullness, and finally owner pending-demand fullness. The last rejects with
`RejectionReason.OWNER_PENDING_DEMAND_FULL` (`"owner_pending_demand_full"`) without
running the callable or stream factory. Grants, cancellation, timeouts, and
closure return queued capacity. FIFO remains local to each class, with no
cross-class FIFO or scheduler; equal endpoint aliases on different owners don't
share this budget.

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
The class outcome snapshot's `rejected_pending_demand_full` counts that rejection
separately with the existing saturating-counter behavior, as does
`rejected_owner_pending_demand_full` on the class whose request was rejected.

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

## Measure Admission Waits And Correlate Launches

Configure both observers to receive decisions and the launches or preflight refusals they admit. They
can use the same callable or `BoundedExporter`:

```python
from pywhales import DockerClient
from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    SaturationPolicy,
)
from pywhales.telemetry import (
    AdmissionDecisionRecord,
    LaunchRecord,
    RefusalRecord,
    TelemetryRecord,
)

records: list[TelemetryRecord] = []
client = DockerClient(telemetry_observer=records.append)
control = PressureClass("control")
owner = AdmissionOwner(
    EndpointIdentity("primary"),
    {control: ClassLimit(active=2, pending=4, policy=SaturationPolicy.WAIT)},
    telemetry_observer=records.append,
)
containers = owner.request(control, queue_timeout=5).call(client.ps)
decisions = {
    record.admission_id: record
    for record in records
    if isinstance(record, AdmissionDecisionRecord)
}
for record in records:
    if (
        isinstance(record, (LaunchRecord, RefusalRecord))
        and record.admission_id is not None
        and record.admission_id in decisions
    ):
        decision = decisions[record.admission_id]
        print(record.attempt_id, decision.outcome, decision.duration_seconds)
```

The owner's `telemetry_observer` is keyword-only and defaults to `None`, separately
from the client's launch observer. Each activated request produces one immutable
`AdmissionDecisionRecord` (`phase="admission"`): opaque `admission_id`, closed
`outcome`, and nonnegative monotonic `duration_seconds` from activation to the
decision, including lock/queue wait. Schema-8 outcomes are `granted`, `busy`,
`queue_full`, `pending_demand_full`, `owner_pending_demand_full`, `closed`,
`queue_timeout`, `cancelled`, and `failed` (including reentry failure).
`granted` records admission, not successful execution of the callable. Cancelling
an inert request before activation emits nothing. With no owner observer, no
admission IDs, callbacks, or extra clock reads are added; snapshot counters keep
their existing meanings.

Decisions are committed under the owner lock but observers run outside all owner
and stream locks, on the deciding thread (which may be the cancelling/closing
thread). Ordinary observer exceptions don't change admission results or interfere
with permit release. Keep callbacks quick and thread-safe as for execution telemetry.

Launches and preflight refusals during the admitted call, stream factory, source entry, every `next()`,
and source exit carry its ID. Nested different owners use the innermost ID and
restore the outer ID on exit; an inner owner without an observer uses `None`.
Outside admission the ID resets to `None`. Returning an unconsumed stream from
`call()` doesn't extend admission: use `stream()` for lifecycle correlation and
permit ownership. IDs correlate local records, not endpoints or daemon work;
decision records contain no endpoint alias, pressure-class name, or exception
text. With an exporter, drain owned streams before shutdown and tolerate missing
joins when individual records are dropped.
