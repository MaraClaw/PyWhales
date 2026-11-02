# Bounded endpoint admission

`pywhales.admission` is an opt-in application layer for limiting work submitted
to an endpoint. It doesn't hook into `DockerClient` automatically, and its names
aren't exported from the `pywhales` package root. Direct SDK calls retain their
existing behavior. Only operations explicitly routed through an owner count
toward its limits.

## Choose an owner and pressure classes

An operator assigns each endpoint an identity and chooses the pressure classes,
limits, and operation-to-class mapping. PyWhales doesn't classify operations
automatically. A class has separate active and pending limits and a saturation
policy. Endpoint and class identifiers are safe tokens: at most 64 ASCII
characters, starting with an ASCII letter or digit, followed only by ASCII
letters, digits, `_`, `.`, or `-`. Don't put credentials or other confidential
information in them.

Use the same `AdmissionOwner` instance for all aliases of a daemon when they
must share a budget. Equal identity strings on separate owners don't combine
their counts. An owner covers only work sent through that instance in one
process. Multiple processes need separate budget shares or external coordination
before claiming an aggregate limit.

The following numbers and queue timeout are illustrative, not recommended
budgets. Choose them for your workload and endpoint. Running these examples can
create a container or pull images; use an appropriate Docker environment.

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
stream_class = PressureClass("transfer")
owner = AdmissionOwner(
    EndpointIdentity("primary"),
    {
        control: ClassLimit(active=2, pending=4, policy=SaturationPolicy.WAIT),
        stream_class: ClassLimit(active=1, pending=2, policy=SaturationPolicy.WAIT),
    },
)

container = owner.request(control, queue_timeout=5.0).call(
    lambda: client.container.create("alpine:latest")
)
```

The active limit counts logical outer operations, not internal image futures or
actual CLI processes. A composite operation holds one permit for its entire
scope, including preflight such as an image pull. Its child work isn't admitted
again. This bound doesn't guarantee a cap on Docker's internal work.

Each reservation also carries an application-defined `demand`, defaulting to
`1`. `ClassLimit(..., demand_capacity=50)` optionally caps a class's total
active demand separately from its active-slot count; the default `None` leaves
aggregate demand unbounded. `owner.request(control, demand=3)` reserves 3
demand units up front instead of the default 1. Demand validation rejects a
`bool`, a non-`int`, a non-positive value, or a value exceeding a configured
`demand_capacity`. Reservations only account for participating application
work; they aren't a cap on daemon memory or CPU.

`ClassLimit(..., pending_demand_capacity=20)` independently caps the sum of
declared demand retained in the pending queue. Its default `None` is unbounded;
nonnegative integers, including zero, are accepted, but booleans are rejected.
The limit applies only when queueing: even demand greater than this limit can
receive an immediate grant when active capacity is available and the queue is
empty. Zero therefore permits immediate work but retains no waiting demand.

## Waiting, rejection, and cancellation

When active capacity is unavailable, `SaturationPolicy.WAIT` waits in a bounded
FIFO pending queue. `SaturationPolicy.REJECT` rejects immediately instead of
waiting. There is no overflow queue. An optional `queue_timeout` sets a separate
deadline for
waiting for admission and has a distinct timeout outcome. It doesn't change or
consume the existing local launch timeout, which still starts at launch.

After closure and same-owner reentry checks, admission tries an immediate grant.
Otherwise `REJECT` reports `RejectionReason.BUSY`; `WAIT` checks pending request
count first (`QUEUE_FULL`), then pending demand (`PENDING_DEMAND_FULL`, value
`"pending_demand_full"`). Exact demand fits are accepted atomically; a refused
request is never retained and never invokes its operation or stream factory.
FIFO remains head-only: a large head blocks smaller followers, even if they fit
the active demand budget. Cancellation, queue timeout and closure remove queued
demand; active resizing does not change the queued total.

`owner.request(...)` creates an inert, single-use handle. Calling `call()` or
`stream()` on it attempts admission and runs the supplied callable only after a
permit is acquired. A controller thread can retain the handle and call
`request.cancel()` while another thread is waiting. Successful pending
cancellation ensures the callable, its preflight, and its CLI work don't start.
Once the request has started, `cancel()` returns `False` and doesn't undo the
operation or roll back daemon-side changes.

## Streaming operations

Use `stream()` with a factory that returns a context manager yielding an
iterator. Stream setup runs under the permit, and the permit stays with the
wrapper until exhaustion, failure is observed, or the wrapper is closed. Use the
returned wrapper as a context manager, particularly when stopping early:

```python
with owner.request(stream_class).stream(
    lambda: client.image.pull(["alpine:latest", "busybox:latest"], stream_logs=True)
) as records:
    for image, chunk in records:
        print(image, chunk)
        break
```

A bare `break` doesn't close the wrapper. An idle stream whose source has failed
still retains its permit until the failure is observed or the wrapper is closed.
Closing uses a clean context exit and can deliver a source error. If the body
and cleanup both fail, the errors are grouped with the original body error first.
Closing local streams doesn't roll back daemon-side changes.

Some SDK stream shapes are generators rather than context managers. Wrap those
in `contextlib.closing` inside the factory. For example, an empty image pull
returns a generator:

```python
from contextlib import closing

with owner.request(stream_class).stream(
    lambda: closing(client.image.pull([], stream_logs=True))
) as records:
    for image, chunk in records:
        print(image, chunk)
```

Other SDK methods may return `None` for some inputs. Adapt that return shape
inside the admitted factory; don't call the SDK before admission to discover
the shape. `stream()` requires a context manager yielding an iterator.

## Resizing demand

An active reservation from `call()` or an open `stream()` can adjust its demand
in place with `try_resize(demand)`, available on both the original
`AdmissionRequest` and on `AdmittedStream`; they share one reservation, so
either handle resizes it. Pass the new total demand, not a delta. Shrinking
always succeeds and wakes FIFO waiters that might now fit. Growing returns
`False`, unchanged, if the class has any pending request or its
`demand_capacity` can't accommodate the increase; equal demand always succeeds.
`try_resize` never waits. It raises `ValueError` for invalid or over-capacity
demand, and `RuntimeError` before admission, after release, or once a stream
begins closing, including through the original request handle at that point.

## Lazy sequences and observation

For sequential work, `iter_calls()` consumes callables lazily. Each `next()`
consumes at most one input and runs it through admission. It isn't a parallel
scheduler and doesn't create a future per item.

```python
names = ["alpine:latest", "busybox:latest"]
calls = (lambda name=name: client.image.inspect(name) for name in names)
for image in owner.iter_calls(control, calls):
    print(image)
```

Owner snapshots expose bounded counts and admission outcomes without endpoint
credentials, command arguments, or output payloads. Each `ClassSnapshot` also
carries `demand`, a `DemandSnapshot` of exact active and pending demand totals
plus their own high-water marks, kept separate from the class's active/pending
request counts and their high-water marks. Values printed or logged by your
application remain your responsibility.

`ClassSnapshot.outcomes.rejected_pending_demand_full` counts only queue-demand
refusals, separately from `rejected_full` (pending request count) and
`rejected_busy` (the reject policy). Like the other outcome counters it saturates
at the uint64 ceiling and marks `saturated`. This counter is appended with a
zero default so existing positional `OutcomeCounts` arguments keep their meaning.

Reentering the same owner from an admitted operation fails in the calling
context. Avoid nested admission to that owner; treat the outer composite as one
operation. The owner can't detect arbitrary cross-thread dependency cycles, so
controllers must avoid those themselves.
