# Sanitized execution telemetry

Telemetry is optional. Set `telemetry_observer` on `DockerClient` or
`ClientConfig` to receive immutable `LaunchRecord`, `CompletionRecord`, and
`RefusalRecord` objects, as well as observed admission decisions,
from `pywhales.telemetry`. These names aren't exported from the package root.
There is no monitoring dependency, global observer, background worker, or network
export by default. Without an observer, command results and errors are unchanged.

```python
from pywhales import DockerClient
from pywhales.telemetry import CompletionRecord, TelemetryRecord


def observe(record: TelemetryRecord) -> None:
    if isinstance(record, CompletionRecord):
        print(record.family, record.outcome, record.duration_seconds)


client = DockerClient(telemetry_observer=observe)
```

Construction is inert. The observer is snapshotted by reference at each local
launch, alongside the client's execution policy. Later config mutations affect
new launches only, not a running stream's completion callback. Commands built
from that client retain the policy through ordinary list composition, shallow
copy, and deep copy. A supplied `client_config` takes precedence over constructor
settings, including `telemetry_observer`.

## Version 7 schema and confidentiality

The vocabulary is closed. `schema_version` is `7` for every record kind; execution
`phase` is `launch`,
`completion`, or `refusal`; `family` is `captured`, `stream`, `archive_load`, or
`archive_save`. Family describes the shared execution path, not an operation
inferred from argv. `archive_load`/`archive_save` cover only the filename-free
binary transfer paths: `image.load()` given bytes or a byte iterable, and
`image.save()` with no `output` path. Filename-based `--input`/`--output`
transfers still use the existing `captured`/`stream` paths, not a separate
family. `image.save()`'s archive path returns a lazy generator; if it's
discarded unconsumed, its body never runs and no launch or completion record is
emitted at all. Completion `outcome` is exactly one of `success`, `failure`,
`timeout`, `closed`, `cancelled`, or `limit`. `cancelled` is an
`ObservationCancelled` and `limit` is an `ObservationLimitError` raised after the
launch record, including a `max_total_stream_bytes` overflow and an operating
system `native_argv` refusal. Both are scopes stopped on purpose, so dashboards
can chart them apart from `failure`. `ObservationTimeout` and `DockerTimeout`
remain `timeout`, and `KeyboardInterrupt` remains `failure`.
Each launch attempt has an opaque random `attempt_id`, shared with its completion;
it carries no command, endpoint, credential, PID, or remote-operation meaning.

Records never include commands or arguments, endpoint addresses, contexts,
environment keys or values, stdin, stdout/stderr payloads, exception text, or
free-form labels. `raw_diagnostics=True` does not relax this boundary. Explicit
returned output, raw exception attributes, and application-owned logging remain
confidential and outside telemetry's sanitization contract. An application can
leak its own data from its callback or sink; PyWhales doesn't sanitize their code.

Observed `AdmissionOwner` requests emit exactly one `AdmissionDecisionRecord`
when activated, with phase `admission`, an opaque `admission_id`, nonnegative
activation-to-decision `duration_seconds`, and a closed outcome: `granted`,
`busy`, `queue_full`, `pending_demand_full`, `closed`, `queue_timeout`, `cancelled`,
or `failed`. `pending_demand_full` means the queue's declared-demand limit
refused the request; `queue_full` refers to its request-count limit. Neither
retains or invokes the refused work. Decisions include no queue names, endpoint
aliases, demand values, callables or exception text. Callbacks run outside owner
and stream locks; ordinary observer errors do not change the admission result.
An unobserved owner creates no telemetry IDs or telemetry clock reads.

A grant describes acquisition, not subsequent operation success. Launch and
refusal records can correlate to the innermost observed admission ID; completion
records correlate through their launch attempt ID. Dormant cancellation emits
no decision. Schema 7 extends the admission outcome vocabulary; consumers of
schema 6 must update their closed-outcome handling before accepting schema 7.

Launch is emitted immediately before an attempted local spawn, after shared
preflight validation. It does not prove that spawn succeeded. A spawn failure has
a failure completion with unavailable byte counts, except an operating system
argument-size refusal inside an observation, which completes as `limit`.
An observation cancellation, deadline or argv/environment limit rejected by the
launch preflight emits exactly one `RefusalRecord` with outcome `cancelled`,
`timeout` or `limit`, instead of a launch/completion pair. It has an opaque
`attempt_id` and optional innermost observed `admission_id`, but no duration,
byte counts or return code. Launch records carry the same optional admission ID;
completion records join their launch by attempt ID. Earlier argument validation
and environment resolution failures do not emit a refusal. No observer means no
telemetry IDs or clock reads. Lazy saves use the observer and observation active
at first iteration. The shared `run()` and process-stream factories are
covered, as are the direct binary subprocesses behind `archive_load` and
`archive_save`. Other direct subprocess paths may remain uncovered; this is not
a complete audit of every Docker invocation.

Completion adds `duration_seconds`, `stdout_bytes`, `stderr_bytes`, and
`stdin_bytes`. Duration uses a monotonic clock, starting after the launch
callback returns and ending
before completion delivery. It includes local spawn, execution and cleanup; for
streams it also includes consumer pauses until exhaustion, failure delivery, or
close. It is not admission queue wait, Docker daemon task duration, rollout
convergence, or application readiness. Callback time isn't a separate metric.
Completion also includes `return_code`, the native status of the reaped local
process, independent of `outcome`, or `None` when unavailable (including failed
spawn). It does not describe daemon-side work.

Counts measure raw bytes captured or read from each local pipe, before decoding,
newline removal, or application transforms. They include read bytes discarded by
a stream close or a response, stream, or buffer limit, plus any bytes a background watchdog
thread drains in a single final pass after killing an expired or closed child.
They can be partial on timeout, failure, or early closure and do not assert how
much the child or remote workload wrote. A pipe that was read but produced no
bytes reports `0`; inherited/uncaptured output or an unavailable capture reports
`None`, never a fabricated zero. Totals publish only after local process and
accounting-thread cleanup finishes, not at the instant of timeout or closure.

`stdin_bytes` measures bytes accepted by successful writes to the child's
stdin, including partial progress retained before a failure or timeout. It is
`None` when no input was supplied (stdin unused or unobserved) and `0` when
input was supplied but nothing was written yet. It never measures bytes the
child or the remote Docker daemon actually consumed. Launch records have no
duration or byte-count fields because those measurements aren't available yet.

## Stream completion and observer behavior

Each launch attempt has at most one completion. Captured execution completes on
return or failure. A stream completes only when its consumer exhausts it,
observes a terminal error, or closes it, after local process/thread cleanup. Child
exit alone does not complete an unconsumed stream. Always use a context manager
or explicitly close it when stopping early; a bare loop `break` leaves ownership
with the caller and doesn't promise a completion record.

Natural exhaustion with zero exit status is `success`. Natural nonzero exit,
spawn failure, reader error, or a recorded output-size (`buffer_bytes`) error is
`failure`. A recorded local or observation deadline is `timeout`. An observation
cancel token is `cancelled`, and an exceeded `max_total_stream_bytes` is `limit`.
Explicit closure before natural completion is `closed`, even when close wins a
race against a pending natural nonzero exit.

One rule picks the outcome, at terminal delivery and in `close()` alike: it
reports the error the stream delivers, which is a local deadline first, then a
scope stop (cancel token, observation deadline, or stream budget), then a reader
or output-size error. A stop already recorded before close, or one `close()`
itself finds because a token is set, keeps its outcome and its original one-time
error delivery. A close with no recorded stop stays `closed`. Neither `closed`,
`cancelled`, nor `timeout` claims remote cancellation or rollback. Mapping
or context-body exceptions still close the local stream without being replaced
by observer exceptions; an otherwise healthy stream then reports `closed`.

Observers run synchronously on the launching/consuming/closing thread, outside
stream locks. They must be quick, thread-safe across concurrent clients/streams,
and non-reentrant. Ordinary `Exception` from either callback phase is contained,
without logging its details, and cannot replace a command result or original
exception. Process-control `BaseException`, blocking, reentrant callbacks, and
adversarial side effects such as changing execution state are outside this
contract. Slow callbacks delay their calling thread; telemetry is not a sandbox.

## Bounded, application-owned export

`BoundedExporter` is an optional thread-safe buffer, not a worker or transport.
The application must choose a positive integer `capacity` and the explicit
`drop_policy="drop_newest"`. Admission never calls the sink: when full, the newest
record is discarded and `dropped` increases. Launch and completion records can
be dropped independently, so exported pairs are not guaranteed. `queued` counts
pending records; `export_errors` counts sink failures discarded without retry.

```python
from pywhales import DockerClient
from pywhales.telemetry import BoundedExporter, TelemetryRecord


def sink(record: TelemetryRecord) -> None:
    # Hand off only these sanitized fields to an application-owned backend.
    print(record)


exporter = BoundedExporter(sink, capacity=256, drop_policy="drop_newest")
client = DockerClient(telemetry_observer=exporter)
try:
    # Run application work here; periodically flush on an application-owned thread.
    exporter.flush()
finally:
    exporter.shutdown(flush=True)
```

The capacity above is illustrative, not a recommended production budget.
`flush()` exports one finite snapshot and returns the number of successful sink
calls. New arrivals wait for a later flush. One snapshot of at most `capacity`
records may coexist with at most `capacity` queued records. Concurrent flushes
are serialized. A sink's ordinary exceptions are counted and contained without
rendering them; failed records are not retried. A sink must not recursively call
`flush()` or `shutdown()`.

The application owns flush scheduling, sink I/O timeouts and shutdown. `flush()`
and `shutdown()` can block on the sink: a finite queue is not a sink-latency
guarantee. `shutdown(flush=True)` stops admission and exports the final snapshot;
`shutdown(flush=False)` counts pending records as dropped and discards them.
Later arrivals are dropped. Repeated shutdown is safe and exports nothing.
Close or exhaust owned streams before exporter shutdown if their completion
records should still be admitted. No implicit retry, network I/O, worker thread,
or process-exit hook is installed.
