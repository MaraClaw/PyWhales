# Admission Contracts

Opt-in, in-process FIFO admission around caller-supplied work. It adds no executor, doesn't change SDK child scheduling, and can't cancel daemon-side work. Stream and process lifecycle rules are in [the shared runtime guide](../AGENTS.md).

## Local Navigation

- `__init__.py`: public names live in `__all__`; everything else is internal.
- `_contracts.py`: frozen identities, `ClassLimit`, snapshots, and the `validate_*` helpers. `_errors.py`: `AdmissionError` subclasses and the bounded `RejectionReason`; messages carry no operation or connection payloads.
- `_owner.py`: `AdmissionOwner` owns one `Condition`, the per-class buckets, and the closed flag. `_acquire`, `_try_resize`, `_begin_close`, `_cancel`, and `_release` handle lifecycle transitions; `_telemetry.py::close_pending` handles owner closure.
- `_state.py`: `Ticket` and `Bucket` hold the FIFO deque, active and pending demand, high waters, and `Outcome` counters, all mutated under the owner lock.
- `_request.py`: `AdmissionRequest`. `_stream.py`: `AdmittedStream`. `_guard.py`: reentry protection and admission correlation scopes. `_telemetry.py`: activation, exactly-once decision commitment, and unlocked delivery.

## Owner And Queue

- Share the exact `AdmissionOwner` to share limits. Equal `EndpointIdentity` aliases never merge owners, and there is no global registry.
- A request is inert until `call()` or `stream()`. Only queued tickets are registered, user callables aren't retained, and `queue_timeout` starts at activation. `_acquire` computes the deadline before taking the lock, so lock wait counts against it.
- Grants are FIFO and head-only. The fast path needs an empty queue; a waiter proceeds only as `pending[0]` with active and demand capacity free. A head that doesn't fit blocks smaller followers on purpose. `demand_capacity` is per class, and `None` leaves demand unbounded.
- `pending_demand_capacity` separately bounds queued demand: `None` is unbounded, exact integers >= 0 are valid, and bool rejects. Check closure/reentry, immediate grant, busy rejection, pending-count fullness, then pending-demand fullness atomically before enqueue. Exact fit succeeds; zero still permits immediate grants. Refusal uses `pending_demand_full` without invoking a callable or stream factory.
- Keyword-only `AdmissionOwner(..., owner_pending_demand_capacity=...)` bounds the sum of queued demand across its classes under the same condition lock. `None` is unbounded; exact integers >= 0 are valid, never bool. Check it after class pending-demand fullness and before enqueue; refusal is `owner_pending_demand_full`. Exact fits succeed and immediate grants bypass both queue-demand limits, including zero. This allowance is separate from pending count and active `owner_demand_capacity`.
- Keyword-only `owner_demand_capacity` is `None` or a positive exact integer (never bool) bounding active declared demand summed across classes. `_owner_fits` runs under the condition lock for fast grants, queued heads and resize growth, alongside class limits. `request()` and `try_resize` raise `ValueError` for demand above it, so a request that can never fit never queues. The default leaves behavior unchanged; owners stay independent and class FIFO stays head-only.
- With `owner_demand_capacity` set, growth also returns `False` while any class has a pending request; unset, only the growing class's queue blocks it. Shrink or release notifies waiters in every class, and failed growth leaves everything unchanged.
- Under `REJECT`, a request whose class is otherwise ready but whose owner allowance is full is refused as `owner_demand_full`; class occupancy/demand or an existing class queue still selects `busy`. `WAIT` retains `queue_full`, `pending_demand_full`, then `owner_pending_demand_full` checks before enqueue and waits for active allowance. Cancellation, timeout and `close()` never release active demand; it frees only at `_release` (after stream source exit and any in-flight read, even if cleanup fails).
- `try_resize` takes total demand, not a delta. Growth obeys class and owner capacity plus the pending-work guard above; shrinking notifies waiters across classes.
- Every terminal path sets `ticket.done` and records one `Outcome`; `Bucket.dequeue` is idempotent. `close()` rejects and clears pending tickets and keeps active permits; afterward `_cancel` returns `False`. A dormant request rejects only when activated.
- `Outcome` values index `Bucket.counts`, and `Bucket.snapshot` builds `OutcomeCounts` positionally. A new outcome must update `OutcomeCounts`, `snapshot`, and the contract tests together. Counters saturate at `UINT64_MAX` and set `saturated`.
- `OutcomeCounts.rejected_pending_demand_full` follows `saturated`; `rejected_owner_pending_demand_full` and then `rejected_owner_demand_full` are appended after it, preserving prior positional meanings. Each saturates at `UINT64_MAX` and sets `saturated`; snapshots retain per-class active/pending demand separately. Pending demand drops exactly once on grant, cancellation, timeout or closure and frees the owner-wide queued allowance too.
- `wait_drained` counts active permits only, never closes, runs the reentry check, and clamps each wait to `TIMEOUT_MAX`. Lock wait counts against its deadline too.
- `iter_calls` builds one request eagerly to validate arguments, then pulls exactly one callable per `next()`: no prefetch, no backlog.

## Reentry And Streams

- `operation_scope` adds the owner id to a `ContextVar` and sets telemetry's current admission ID around user operations, stream factory and source entry, each `next()`, and source exit. Both tokens reset on exit; nested different owners use the innermost ID (`None` for an unobserved owner). Same-owner nesting in that context raises `AdmissionReentryError`; cross-thread nesting is unsupported.
- `AdmittedStream` holds one permit until source `__exit__` and any in-flight read finish. `_begin_close` is the only call that takes the owner lock while the stream lock is held, so don't add the reverse order. It ends resize eligibility without releasing the reservation.
- Source exit runs before waiting on an in-flight read so it can wake a blocked `next()`. `close()` is idempotent, concurrent closers wait for one cleanup owner, and a bare loop `break` keeps the permit while the stream is reachable.
- A dropped stream is closed after garbage collection by the shared reclaimer thread through the same `close()` path: source exit, any in-flight read, then `_release`. Its finalizer only enqueues the internal core and never enters `_owner.py`. The [runtime guide](../AGENTS.md#observation-and-subprocesses) covers the handle and core split.
- `__exit__` returns `False`. A cleanup failure after a body failure joins it in a `BaseExceptionGroup`; interrupted cleanup waits keep waiting and are combined, not dropped. Failed setup releases without exiting a half-entered source.
- One stream permit covers every child of its source, including composite image streams. Don't admit those children again through the same owner.

## Admission Telemetry

- `AdmissionOwner(..., telemetry_observer=...)` is keyword-only and optional. Every activated, observed request commits one schema-11 `AdmissionDecisionRecord`: opaque admission ID, nonnegative seconds from activation to decision, and one of `granted`, `busy`, `queue_full`, `pending_demand_full`, `owner_pending_demand_full`, `owner_demand_full`, `closed`, `queue_timeout`, `cancelled`, `failed`. A dormant cancellation emits nothing; decisions describe acquisition, not operation completion.
- Fast grants, saturation/full-queue rejects, waiting grants/timeouts, pending closure, cross-thread cancellation, and reentry failure share exactly-once commitment under the owner condition. The winning transition owns delivery, so an unwinding waiter cannot emit again. Snapshot counters retain their existing meanings.
- Deliver outside every owner and stream lock, including when source cleanup closes or cancels a pending request. Ordinary observer errors are swallowed by shared `deliver()` without changing admission results or permit release. Never record endpoint aliases, pressure-class names, callables, or exception text.
- No observer means no admission ID, callback, or added clock read. Preserve the existing `_owner.monotonic` scripts and queued-wait signals. Launch correlation requires the client's own execution observer too; record definitions and exporter rules live in [the runtime guide](../AGENTS.md#telemetry).

## Tests

Paths are under `tests/pywhales/`. None use the `docker_client` fixture: Docker call sites are patched, and real children are local `sys.executable` processes.

- `test_admission_contracts.py`, `test_admission_producer.py`: validation, frozen snapshots, saturation, pull-only `iter_calls`.
- `test_admission_calls.py`, `test_admission_waits.py`, `test_admission_weighted_queue.py`: zero-work rejection, FIFO bounds, cancel, queue and drain deadlines, demand ordering.
- `test_admission_pending_demand.py`: queue-demand exact/zero bounds, rejection precedence, contention, cancellation/closure, saturating counters and stream lifecycle cleanup.
- `test_admission_owner_pending_demand.py`: cross-class queue-demand bounds and reuse, immediate-grant bypass, rejection precedence, independent owners, terminal cleanup, saturating counters and unlocked schema-11 refusal delivery.
- `test_admission_owner_demand.py`, `test_admission_owner_demand_streams.py`: owner-wide active demand: validation, fast/queued grants, resize and cross-class growth exclusion, FIFO, cancellation/close, stream retention through source exit and in-flight reads, saturating counter and unlocked refusal telemetry.
- `test_admission_streams.py`, `test_admission_stream_races.py`, `test_admission_processes.py`: cleanup order, error grouping, one permit over real children.
- `test_admission_stream_reclaim.py`: dropped streams exit their source before release, and collection under the owner or stream lock does no cleanup inline.
- `test_admission_resize.py`, `test_admission_resize_races.py`, `test_admission_concurrency.py`: reservation, close, cancel, and grant races.
- `test_admission_telemetry.py`, `test_admission_telemetry_correlation.py`, `test_admission_telemetry_locks.py`: decision outcomes, disabled overhead, observer isolation, nested call/stream correlation, cross-thread cancellation, and unlocked observer delivery.
- `test_admission_types.py` holds `assert_type` checks for `ty`; pytest collects nothing from it.
- Tests script `_owner.monotonic` with fixed call counts and wrap `owner._condition.wait` as the "queued" signal. Adding a clock read or moving a wait site in `_owner.py` needs matching test updates.

```bash
uv run --locked pytest tests/pywhales/test_admission_*.py tests/pywhales/test_telemetry*.py -q
```

That selection includes concurrency and local-process cleanup tests but needs no daemon.
