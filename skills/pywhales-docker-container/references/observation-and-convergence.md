# Observation Budgets And Convergence

Import bounded observation/inventory APIs from the `pywhales` package root,
`bulk_reload` from `pywhales.client_config`, and convergence APIs from
`pywhales.convergence`. All are opt-in; none changes ordinary `DockerClient`
calls automatically.

## Contents

- [Bounding batch inspection](#bounding-batch-inspection)
- [Sharing one IO budget](#sharing-one-io-budget)
- [Manifest lists](#manifest-lists)
- [Bounding streams and uploads](#bounding-streams-and-uploads)
- [Waiting for convergence](#waiting-for-convergence): [Swarm](#swarm-service-or-stack), [Swarm nodes](#swarm-node-readiness), [Swarm jobs](#swarm-jobs), [Compose](#compose-project), [standalone containers](#standalone-containers), [outcomes](#outcomes-and-budgets)
- [Optional application reconciliation](#optional-application-reconciliation)

## Bounding Batch Inspection

For pinned container, image, network or volume tables, see
[Hydrated listings and snapshot lifetimes](client-and-errors.md#hydrated-listings-and-snapshot-lifetimes).
These listing snapshots differ from detached inventory records below.

```python
from threading import Event
from time import monotonic

from pywhales import (
    DockerClient,
    InspectionPolicy,
    InventoryRequest,
    capture_inventory,
    observation,
)

client = DockerClient()
policy = InspectionPolicy(
    max_references=100,
    max_argv_bytes=32768,
    max_response_bytes=1_048_576,
    max_total_references=200,
    max_total_response_bytes=4_194_304,
    max_total_io_bytes=8_388_608,
    max_total_launch_attempts=8,
    max_concurrent_children=2,
)
cancel = Event()
request = InventoryRequest(
    kind="container",
    references=("authorized-container-name",),
    client_config=client.client_config,
)

with observation(policy, deadline=monotonic() + 30.0, cancel=cancel):
    snapshot = capture_inventory([request], policy=policy)
```

`InspectionPolicy(max_references=..., max_argv_bytes=..., max_response_bytes=...)`
requires three positive integers; there's no default policy. Optional
`max_total_references`, `max_total_response_bytes`, `max_total_stream_bytes`,
`max_total_stdin_bytes`, `max_total_io_bytes`, `max_total_launch_attempts`, and `max_concurrent_children`
default to `None`. An unset source-specific output total leaves that account
unbounded; shared IO can still cap those reads. Unset stdin still refuses
captured input inside an
observation unless an enclosing scope supplies a stdin budget. Booleans, zero,
negative values, and non-exact integers are invalid for every policy limit.
The illustrative limits above aren't a universal sizing recommendation. `observation()`
is a context manager applying those limits, plus an optional absolute
monotonic `deadline` and a `cancel` `Event`, to synchronous list/inspect
calls executed through it. Nested scopes can only tighten the active limits,
never loosen them, and the scope applies to the current context only; it
doesn't automatically propagate to worker threads you spawn inside it.

`max_total_launch_attempts` bounds accepted local CLI launch reservations, not
successful children. One reservation follows cancellation, deadline and argument
preflight for captured calls, streams and both archive directions. Each enclosing
account is checked and debited atomically; exhaustion raises
`ObservationLimitError` with reason `total_launch_attempts` without charging any
account, spawning a child or reading upload input. Accepted reservations are never
refunded after interruption, spawn failure or CLI failure. Nested scopes cannot
reset an outer allowance; explicitly copied contexts share its consumption.
A separate scope created independently in each thread does not share a budget.
Lazy saves reserve at first iteration, and their separate eager inspection costs
its own captured reservation.

`max_concurrent_children` separately caps owned local CLI children until reap,
not daemon workloads, output buffers, or open stream/admission handles. It is
`None` (unbounded) or a positive exact integer. Nested scopes enforce every
enclosing synchronized account; explicitly copied contexts share those accounts.
Saturation raises `ObservationLimitError(reason="concurrent_children")` without
waiting, spawning, reading upload input, or consuming any attempt or slot.
Accepted launches debit attempt and child accounts atomically. A failed spawn
consumes its accepted attempt but returns its slot. A successful spawn keeps the
slot until the local child is reaped, not merely until pipe EOF or scope exit.
After reap, output may still be buffered and the stream or admission permit may
remain open; close or exhaust owned streams anyway. Lazy `save` reserves under
the scope active at first iteration, while eager inspect is charged separately
under its own launch scope. Inventory's sequential chunk plan doesn't reserve
child slots for the whole plan; each launch must fit at its actual preflight.

The scope's restrictions belong to the shared synchronous captured path (the
path list/inspect-style calls use). Inside a scope, that path rejects `tty`,
uncaptured output, and `input` without a stdin budget with `ValueError`, and
charges captured stdout/stderr bytes to the response budgets. Streams and archive
transfers follow a separate launch lifecycle, described under the limits below. Don't use
`observation()` as a general command sandbox.

`capture_inventory(items, *, policy)` accepts a mix of `InventoryRequest`
objects and existing resource handles, and returns an `InventorySnapshot`
whose `records` tuple holds one `InventoryRecord` (`kind`, `id`, `payload`) per
requested occurrence, in request order, duplicates kept. Each `payload` is a
deep copy, independent of every other record and of the input handles, so
mutating one changes nothing else. Capture never changes an existing handle's
own cache. Supported kinds are thirteen: `container`, `image`, `network`,
`volume`, `node`, `service`, `config`, `secret`, `task`, `plugin`, `context`,
`builder`, and `manifest_list`. `InventoryRequest.references` must be a nonempty tuple of
nonempty strings, else `ValueError`. Empty input launches no CLI. Request
shape, total reference count, and each reference's argv and environment limits
are checked for every group before the first inspect launch.
The complete chunk plan must also fit every enclosing launch account's remaining
allowance: one launch per ordinary chunk, one builder listing per group and one
per singleton manifest inspect. This preliminary check consumes no reservations.
Another worker can consume capacity afterward, so the authoritative per-launch
check can still stop a later inspection. Detached capture and staged bulk-cache
publication retain their failure behavior; there is no whole-inventory reservation.

Builders have no batch inspect command, so they work differently. All builder
requests sharing the same `ClientConfig` object use one strict
`buildx ls --format "{{json . }}"` listing per capture or reload call, and names
never reach argv. Each
requested name must match a listed builder name exactly; an absent name raises
`ParsingError`, and duplicate requests each get their own record, in order. The
listing is parsed whole: node rows repeat a builder's line byte for byte and
collapse, but a non-builder line, a null or empty name, or two different lines
under one name rejects the entire listing. Error text omits builder names
unless the client sets `raw_diagnostics=True`. Because no name enters argv,
`max_references` and `max_argv_bytes` neither chunk nor grow with builder
requests, but each requested name still counts toward `max_total_references`,
and the listing's stdout/stderr bytes are charged to `max_response_bytes` and
`max_total_response_bytes` like any captured call.

`bulk_reload(handles, policy=policy)` from `pywhales.client_config` refreshes
existing handles of the same thirteen kinds, `Builder` and `ManifestList` included. It validates
every input kind and identity before any lookup, groups by the exact client
configuration object plus kind, and inspects each distinct handle once; the
same handle object passed twice is published once, while separate handle
objects for one resource each receive a snapshot. It stages and identity-checks
every group before updating any handle's cache or timestamp, so a failure in
any group leaves all caches unchanged. Publication then runs handle by handle,
which isn't a daemon transaction and isn't atomic to concurrent readers. When
`policy` is omitted, an active `observation()` scope applies instead, and
outside such a scope its existing unbounded behavior is unchanged.

### Manifest Lists

Use registry references, not local image IDs, for `manifest_list` inventory:

```python
request = InventoryRequest(
    kind="manifest_list",
    references=("registry.example.com/team/app:release",),
    client_config=client.client_config,
)
snapshot = capture_inventory([request], policy=policy)
```

Requests use secure inspection. Existing `ManifestList` handles are accepted
too and retain their own `insecure` setting; there's no request-level insecure
option. Each reference gets a singleton manifest inspect, not a batch command.
`record.id` binds the exact requested reference, not a digest from the response.
Records stay detached and ordered, with a separate record for each duplicate.

As with other kinds, `bulk_reload` groups by the exact `ClientConfig` object,
not equality, and stages and validates all groups before publishing any cache
updates. A failure leaves all input caches untouched. Public
`client.manifest.inspect()` and an individual `ManifestList.reload()` keep their
existing lenient behavior; don't assume this bounded-capture validation there.

The per-command reference limit bounds each inspect batch; aggregate reference
accounting counts all requested occurrences, including duplicates, in one
inventory/bulk-reload call. The response limit counts combined raw stdout/stderr bytes per
invocation. Exceeding a limit raises `ObservationLimitError`; an expired
deadline raises `ObservationTimeout`; a set `cancel` event raises
`ObservationCancelled`. Failures stop/reap an active owned local CLI when one
exists; some limits reject before launch. Nothing rolls back daemon-side work,
and the regular, selected-launch
`DockerTimeout` from `default_timeout`/`timeout=` is unaffected and can still
fire independently.

### Sharing One IO Budget

`max_total_io_bytes=None` leaves this additional total unset. A positive exact
integer adds one shared allowance for actual captured stdout/stderr reads,
stream stdout/stderr reads (including read-ahead), archive caller reads, and
managed stdin writes from captured input and byte/iterator archive loads. Only
bytes accepted by successful local pipe writes count as stdin, including partial
writes. Bytes merely yielded by an input producer don't count.

Each bounded scope adds an account shared across nested scopes and explicitly
copied contexts. All enclosing IO accounts are checked and charged atomically
with each pipe read or accepted write, so concurrent readers and writers cannot
spend the same remaining allowance. Independently created scopes don't share it.
Started streams and archive transfers retain their accounts after scope exit.

Output may read one detection byte past the tightest allowance before raising
`ObservationLimitError(reason="total_io")`; exact-budget EOF succeeds. After
overflow no further reads or writes spend that account. Managed writes never
send bytes beyond either the stdin or IO allowance. Source-specific response,
stream, and stdin limits remain in force and win a same-operation tie; shared
IO does not replace any of them. Captured input still requires an explicit
`max_total_stdin_bytes` in the active or an enclosing scope: IO alone doesn't
enable it.

Archive cleanup/watchdog drains and post-exit stderr reads used to classify a
Docker failure are excluded from IO spending. Direct or inherited stream stdin
and archive file contents read/written by the CLI through filename arguments
are also excluded; any captured or streamed progress output still counts.
This is managed local pipe accounting, not daemon network traffic or a total
memory cap.

### Bounding Streams And Uploads

Streams and archive transfers (log and progress streams, `load`, `save`) can
retain a scope's lifecycle instead. Started inside a scope, they check
cancellation and the argv and environment limits before launch, keep the
scope's deadline and tokens after the `with` block exits, and kill and reap the
local CLI before raising `ObservationTimeout` or `ObservationCancelled`; a lazy
`save` binds the scope active at its first iteration. Streamed or archive
payload bytes are not charged to response budgets, and these launches aren't
subject to the captured-output rejection above. Multi-image `pull`/`push` run
each task in a copy of the calling context, so a non-streaming
`pull(quiet=False)` inside a scope raises the same `ValueError` for one image
or many, while `stream_logs=True` accepts `quiet=False` there.

Set `max_total_stream_bytes` to bound combined stream/archive stdout and stderr
bytes as pipe readers pull them, including buffered read-ahead. Each enclosing
scope with this field set owns a separate account; each read charges all of them,
even after the launching scope exits. Captured output never consumes this budget.
Overflow stops and reaps the local CLI and delivers `ObservationLimitError` with
reason `total_stream` once. Archive watchdog drains and the stderr read that
selects a Docker exception are uncharged. Leaving the field unset changes no
stream or archive accounting.

Set `max_total_stdin_bytes` to a strictly positive integer to bound uploads across
the captured execution helper's `run(input=...)` calls and archive loads. This
is child-CLI stdin, not a new `input=` argument on `client.run()` (container run).
For public archive uploads, use `client.image.load()` with bytes or a byte
iterable; see [Registry transfers and archives](registry-and-archives.md).
Bytes and cooperative byte iterables are accepted; stdout and stderr drain during
captured uploads. Only
bytes accepted by successful local pipe writes count, including partial writes,
against every enclosing shared account. Empty input consumes nothing and exact
budget EOF succeeds; excess input raises `ObservationLimitError` with reason
`total_stdin`. Concurrent launches sharing a propagated observation context share
the same allowance; declaring a new independent scope in each thread does not
create a shared request budget. Upload accounts remain attached to launched
transfers after scope exit and never consume response or stream budgets. Iterator code must yield
cooperatively: deadlines and cancellation cannot interrupt it, and cleanup waits
for an in-flight iteration before joining the writer.

Aggregate response budgets apply within observation scopes and the bounded
inventory call. Snapshots aren't daemon transactions or total memory caps.

## Waiting For Convergence

`pywhales.convergence` is an opt-in, read-only observer of one fixed target:
a captured Swarm service generation, including replicated and global jobs
(`capture_service` returns a `ServiceTarget`), an explicit Swarm stack
(`StackTarget`), selected Swarm nodes (`capture_nodes` returns a `NodeTarget`),
selected Compose services (`capture_compose` returns a
`ComposeTarget`), or standalone containers (`capture_containers` returns a
`ContainerTarget`). `wait()` takes any of these five target types. Existing mutating calls
never invoke it automatically; capture and wait yourself after submitting the
change you want to confirm. Choose the
Swarm path for `docker service`/`docker stack` deployments and the Compose path
for a `docker compose` project; use standalone capture for explicit container
names or IDs.

### Swarm Service Or Stack

```python
from threading import Event

from pywhales import DockerClient, InspectionPolicy
from pywhales.convergence import ServiceTarget, capture_service, wait

client = DockerClient()
policy = InspectionPolicy(
    max_references=32, max_argv_bytes=65536, max_response_bytes=1_048_576
)
cancel = Event()

target = capture_service(
    client, "full-service-id", policy=policy, timeout=10, cancel=cancel
)
if isinstance(target, ServiceTarget):
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1, cancel=cancel)
```

`client.service.update()`, `client.stack.deploy()`, and rollback retain their
existing behavior. Always pass the exact, immutable service ID returned by your
own submission, not a name; the caller must know the complete, fixed membership
of a stack before building a `StackTarget`, never inferred from a live,
possibly-changing discovery.

`capture_service(client, service_id, *, policy, timeout, cancel=None, approved_node_ids=None)`
returns a `ServiceTarget`, or a typed `Failure`/`Deadline`/`Cancelled`
outcome. It records the service's version, its job iteration for job modes, and
a fingerprint of its desired task template and mode at that instant. Replicated
services need an explicit nonnegative replica count; Global services need
`approved_node_ids`, a nonempty tuple of distinct node IDs, at capture time,
since there's no automatic discovery of "eligible" nodes. Supplying
`approved_node_ids` for a Replicated or ReplicatedJob service is `unsupported`.

Use an authorized service's actual immutable ID in place of the placeholder.
Keep the target's originating `ClientConfig` stable while waiting. Replicated,
Global, ReplicatedJob, and GlobalJob modes are supported; any other mode or a
non-container runtime is `Failure(reason="unsupported")`, and malformed
service/task data fails closed.

`wait(target, *, policy, timeout, poll_interval, cancel=None)` accepts a
`ServiceTarget`, a `StackTarget(services=(...))`, a `NodeTarget`, a `ComposeTarget`, or a
`ContainerTarget` and returns `Success`, `Failure`, `Deadline`, or `Cancelled`. For ordinary
(Replicated and Global) Swarm targets, only current desired-running tasks count;
a changed service `Version` or template fingerprint returns `superseded`;
`paused`/`failed`/rollback `UpdateStatus` states are terminal `update_failed`
failures. Jobs follow their own rules, described next. Snapshot collection isn't
an atomic Swarm transaction, so a final generation recheck reduces, but can't
remove, races from concurrent changes. A successful result proves the captured
task-generation conditions, not application health or readiness.

### Swarm Node Readiness

Use `capture_nodes()` before resuming deployment after node maintenance; it
observes existing nodes without changing availability, roles, or cluster state.
Check that the installed `pywhales.convergence` exports both `capture_nodes`
and `NodeTarget`, and that `wait` accepts that target.

```python
from pywhales.convergence import NodeTarget, capture_nodes, wait

# Reuse client, policy and cancel from the service example. Supply actual exact
# node IDs from the application's selected membership, never names or prefixes.
target = capture_nodes(
    client,
    ("exact-manager-node-id", "exact-worker-node-id"),
    manager_ids=("exact-manager-node-id",),
    require_leader=True,
    policy=policy,
    timeout=10,
    cancel=cancel,
)
if isinstance(target, NodeTarget):
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1, cancel=cancel)
```

`capture_nodes(client, node_ids, *, manager_ids=(), require_leader=False, policy, timeout, cancel=None)`
returns `NodeTarget` or a typed `Failure`/`Deadline`/`Cancelled`. `node_ids` must
be a nonempty tuple of distinct nonempty exact IDs. `manager_ids` must be a tuple
of distinct selected IDs; its default `()` requests no manager-specific checks,
not automatic discovery. Invalid selections, timeouts, or dry-run clients raise
`ValueError` before lookup. Keep the originating config stable and target IDs
out of untrusted output. `require_leader` must be exactly a bool; enabling it
requires a nonempty `manager_ids` subset, validated before Docker lookup. The
frozen target retains that option.

Each selected node must have state `ready` and availability `active`. Designated
managers must also remain managers and report reachability `reachable`;
leadership is ignored by default. Demotion or missing manager status blocks success,
while workers need no manager status. Node version-index changes during recovery
are allowed. `wait` inspects the fixed IDs and requires a second complete
all-clear pass; it never re-lists or replaces membership.

With `require_leader=True`, each complete clear pass must observe exactly one
leader among the designated managers. The leader identity may change between
passes. Within a present manager status, omitted `Leader` means false; explicit
nonboolean values (including null) produce cause-free `Failure(reason="malformed")`
only for designated nodes reporting manager role and status, at capture and wait.
Other nodes cannot satisfy or invalidate the selected leader count. These are
sequential selected observations, not proof of global or continuous leadership.

An exact missing-node response during capture is `Failure(reason="observation")`
with its cause; disappearance during wait is cause-free `superseded`. Unrelated
CLI errors remain observation failures, not disappearance. Malformed projections,
unknown readiness values, or a returned identity mismatch fail closed. Capture
checks identity and wire shape, not readiness, so a recovering node can still be
captured. `Success` proves the selected node conditions at observation time, not
application health or permission to deploy.

### Swarm Jobs

`capture_service` and `wait` handle `ReplicatedJob` and `GlobalJob` services
without any extra parameter. Neither the version string nor `capture_service`'s
signature shows that, so check that the installed `ServiceTarget.mode`
annotation lists `replicated_job` and `global_job` (see
[Usage validation](validation.md)). Ordinary Replicated and Global services
keep the rules above.

Capture:

- A ReplicatedJob service takes no `approved_node_ids`. `MaxConcurrent` defaults
  to 1 and `TotalCompletions` defaults to `MaxConcurrent`; both must be positive
  integers, and any other key in the job mode is `unsupported`.
- A GlobalJob service needs `approved_node_ids` like a Global service, and its
  job mode must be empty.
- The service must report a job status. The captured iteration is
  `JobStatus.JobIteration.Index`, read as 0 when `Index` is missing; a service
  without `JobStatus` is `Failure(reason="malformed")`. Capture also records the
  template's `RestartPolicy.Condition`.

Waiting:

- Only tasks whose `JobIteration` equals the captured iteration count. Tasks from
  other iterations, or with no iteration, are stale and ignored.
- A matching task counts once its state is `complete`; any other state stays
  pending, and desired state isn't consulted. A ReplicatedJob succeeds when
  `TotalCompletions` distinct zero-based completion slots (`0` through
  `TotalCompletions - 1`) are complete. A GlobalJob succeeds when every approved
  node has a completed task; completions on other nodes don't count.
- If the captured restart condition is exactly `none`, a matching task that is
  `failed` or `rejected` ends the wait with `Failure(reason="completion_failed")`.
  It carries the service ID but no state or cause. With any other or a missing
  condition those tasks simply aren't counted, and the wait stays pending until
  the deadline.
- The generation fence is the template and mode fingerprint plus the job
  iteration, with no service `Version` check. A changed fingerprint or a new
  iteration is `superseded`; a `Version` bump alone isn't.
- `UpdateStatus` isn't completion evidence and is ignored for jobs, so it can't
  produce `update_failed` or an ongoing-update blocker.

As with ordinary services, `Success` proves the captured conditions, not that
the job's work was correct.

### Compose Project

```python
from pywhales.convergence import (
    Cancelled,
    ComposeTarget,
    Deadline,
    Failure,
    Success,
    capture_compose,
    wait,
)

# `policy` and `cancel` are the InspectionPolicy and Event from the Swarm
# example. The client's Compose settings select the project.
compose_client = DockerClient(
    compose_files=["compose.yaml"], compose_project_name="demo"
)
services = ("web", "migrate")  # web stays running; migrate must finish

match capture_compose(
    compose_client,
    services,
    completion_services=("migrate",),
    fail_on_exit=True,  # opt in to failing when a long-running member exits
    fail_on_unhealthy=True,  # opt in to eligible running unhealthy failures
    policy=policy,
    timeout=10,
    cancel=cancel,
):
    case ComposeTarget() as target:
        outcome = wait(
            target, policy=policy, timeout=120, poll_interval=1, cancel=cancel
        )
        converged = isinstance(outcome, Success)
    case Failure(reason=reason):
        print(f"Compose capture failed: {reason}")
    case Deadline() | Cancelled() as stopped:
        print(f"Compose capture stopped: {type(stopped).__name__}")
```

`capture_compose(client, services, *, completion_services=(), max_restarts=None,
fail_on_exit=False, fail_on_unhealthy=False, policy, timeout, cancel=None)` returns a
`ComposeTarget` or a typed `Failure`/`Deadline`/`Cancelled`, so check for the
target before waiting. Capture after `compose up` has returned, so the target
fixes the containers you mean to wait for, and keep the client's Compose
settings (files, profiles, project name and directory, env files) stable while
waiting.

- `services` is an explicit, nonempty `tuple` of distinct, nonempty names, never
  discovered. A list, an empty tuple, duplicates, or a client created with
  `compose_dry_run=True` raise `ValueError` before any launch.
- `completion_services` is a tuple of distinct selected names, fixed at capture;
  a list, duplicates, or a name outside `services` raises `ValueError` before
  launch. The default `()` leaves every selected service long-running.
- `max_restarts` is `None` (disabled) or a nonnegative integer, never a boolean;
  invalid values raise before launch. The target freezes this choice. When enabled,
  every long-running member must report a nonnegative integer `RestartCount` at
  capture and each cycle, otherwise the result is cause-free `malformed`. Any
  member above budget keeps its service unsettled even while healthy. This is a
  live blocker, not a permanent failure or a monotonic-counter assumption;
  completion services ignore the restart budget.
- `fail_on_exit` must be a bool; other values raise `ValueError` before launch.
  The target freezes this choice, which defaults to `False` for compatibility.
  Capture does not itself treat an exit as a failure; `wait()` evaluates it.
- `fail_on_unhealthy` likewise requires an exact bool, defaults to `False`, and
  is frozen on the target before launch. Capture records the policy; only
  `wait()` evaluates terminal unhealthy failures.
- Capture lists the selected services' containers with stopped ones included,
  inspects each, and skips one-off `compose run` containers. For every selected
  service the target records the full container IDs and the single
  `com.docker.compose.config-hash`. A hash covers the service configuration, not
  its replica count; the fixed container IDs cover that. Because the target
  holds container IDs, keep it out of untrusted output.
- A selected service with no containers, or with containers carrying different
  hashes, is `Failure(reason="unsupported")`. A missing hash, a container outside
  the selection, or unusable CLI output is `Failure(reason="malformed")` with no
  cause. A CLI failure, missing executable, or exceeded limit is
  `reason="observation"` and carries its cause.
- Every polling cycle re-lists the project and re-inspects the members. An added,
  vanished, or recreated container (any change to the ID set) or a changed
  service label or config hash is `superseded`, so a scale-up or recreate after
  capture ends the wait instead of moving the target.
- An unmarked service's container counts as ready only when it is `running`, not restarting, not
  paused, and any reported health status is `healthy`, even without a configured
  healthcheck. Missing `Health` blocks only when a healthcheck is configured;
  a `NONE` healthcheck test counts as none. By default, an exited container isn't
  running and remains a blocker; mark one-shot services for completion instead.
- A marked service completes only when every captured container is `exited`
  with `ExitCode` 0 and not restarting. Created, running, or restarting
  containers remain unfinished. A non-restarting container exited nonzero ends
  the wait with payload-free `Failure(reason="completion_failed")`: no cause,
  exit code, or container ID is included. An exited completion container with a
  missing or malformed `ExitCode` yields `Failure(reason="malformed")` with no
  cause. A service with no containers is still `unsupported`, not complete.
- With `fail_on_exit=True`, a non-restarting long-running member that has exited
  with code 0 or nonzero produces cause-free `Failure(reason="exited")` on the
  first complete evaluation that sees it, including the confirming pass. A
  failed completion member takes precedence as `completion_failed`; an enabled
  restart budget validates all long-running restart counts before checking for
  exits, so a malformed count yields `malformed`. Restarting members and exited
  members over budget remain blockers rather than producing `exited`. Completion
  rules and Swarm service or stack targets are unchanged.
- With `fail_on_unhealthy=True`, a long-running member reporting exactly
  `unhealthy` produces cause-free `Failure(reason="unhealthy")` only when running,
  unpaused, not restarting, and within any enabled restart budget. A reported
  status counts even without a configured healthcheck. `starting`, missing
  `Health`, and other non-healthy statuses stay blockers under the readiness rules
  above, not terminal unhealthy failures. Completion members ignore health.
  Wire validation comes first, then failed completion, enabled restart-count
  validation, eligible `exited`, and eligible `unhealthy`, across the complete
  selection. The policy applies on first and confirming evaluations; paused,
  restarting, or over-budget members remain blockers. Default-off leaves
  unhealthy members pending and doesn't change Swarm targets.
- When a cycle finds all long-running services ready and completion services
  finished, `wait()` lists and checks the project again. `Success` requires
  that mandatory second pass to satisfy the same conditions and be unchanged.
   It proves the captured conditions at that moment, not application readiness
   beyond the configured healthchecks.

### Standalone Containers

Use this path to observe existing containers without Compose project discovery.
`docker.container.wait()` (also `docker.wait()`) waits for containers to stop
and returns exit codes; convergence `wait()` observes running/healthy state and
designated successful completion, returning typed outcomes. See
[Containers, output, and lifecycle](containers-and-output.md#choose-the-result-type)
for the exit-code API.

Check that the installed package exports `ContainerTarget` and
`capture_containers`, that capture accepts `completion`, `max_restarts`, and
both `fail_on_exit` and `fail_on_unhealthy`, and
that convergence `wait` accepts `ContainerTarget`; see
[Usage validation](validation.md). A version string alone doesn't establish
these capabilities.

```python
from threading import Event

from pywhales import DockerClient, InspectionPolicy
from pywhales.convergence import (
    Cancelled,
    ContainerTarget,
    Deadline,
    Failure,
    Success,
    capture_containers,
    wait,
)

client = DockerClient()
policy = InspectionPolicy(
    max_references=32, max_argv_bytes=65536, max_response_bytes=1_048_576
)
cancel = Event()

# These names identify already-existing containers you're authorized to observe.
target = capture_containers(
    client,
    ("database", "migration"),
    completion=("migration",),
    fail_on_exit=True,  # opt in to failing when a long-running member exits
    fail_on_unhealthy=True,  # opt in to eligible running unhealthy failures
    policy=policy,
    timeout=10,
    cancel=cancel,
)
if isinstance(target, ContainerTarget):
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1, cancel=cancel)
    converged = isinstance(outcome, Success)
elif isinstance(target, Failure):
    print(f"Container capture failed: {target.reason}")
elif isinstance(target, (Deadline, Cancelled)):
    print(f"Container capture stopped: {type(target).__name__}")
```

`capture_containers(client, references, *, completion=(), max_restarts=None,
fail_on_exit=False, fail_on_unhealthy=False, policy, timeout, cancel=None)` returns a `ContainerTarget` or typed
`Failure`/`Deadline`/`Cancelled`. Capture and wait never launch, restart, stop,
or remove containers.

`references` must be a nonempty tuple of distinct, nonempty names or ID prefixes.
`completion` must be a tuple of distinct references drawn from that selection;
its default `()` leaves all members long-running. `max_restarts` is `None`
(disabled) or a nonnegative integer, never a boolean. `fail_on_exit` and
`fail_on_unhealthy` must each be an exact bool and default to `False`. Invalid
selections, completion tuples, restart budgets, failure flags, or timeouts, and
clients configured with `compose_dry_run=True`, raise `ValueError` before any CLI launch.

Capture resolves each reference once to a full lowercase 64-hex container ID.
The target freezes those IDs, completion IDs, restart budget, and failure flags; keep its
originating client configuration stable and its IDs out of untrusted output.
Different references resolving to the same ID produce cause-free
`Failure(reason="malformed")`, as does unusable inspect output. A missing
container during capture is `Failure(reason="observation")` with its cause.
Each wait cycle inspects only the captured IDs, never resolving names again;
a later disappearance or recreate is `Failure(reason="superseded")`, not a
replacement to follow. Compose labels aren't required here; Compose capture
still enforces its service labels, config hashes, and fixed project membership.

The [Compose readiness and completion rules](#compose-project) apply per
standalone container: unmarked members must be running, not restarting or
paused, and any reported health must be healthy; missing `Health` blocks only a
configured healthcheck. Marked members must exit
0 without restarting; a nonzero exit without restarting is cause-free
`completion_failed`, and a missing or malformed exit code is cause-free
`malformed`. Both Compose failure-policy rules and their precedence above apply:
capture does not evaluate exits or unhealthy state as terminal failures, and by
default an exited or unhealthy long-running member remains
a blocker. An enabled restart budget checks each long-running member's current
`RestartCount`, not restarts since capture. Missing, negative, or non-integer
counts are cause-free `malformed`; over-budget readings block success but a
later within-budget reading clears the blocker. Completion members ignore the
budget. After an all-clear, `Success` requires a second complete all-clear pass
over the same IDs. It proves only the captured conditions at that moment.

### Outcomes And Budgets

Swarm task, Compose container and fixed standalone-container inspections batch
up to the active policy's `max_references` per launch. Each chunk checks the
observation before launch and maps exact full IDs in target order. Failed or
malformed batches fall back to ordered single-ID probes; cancellation and scope
deadline expiry stop immediately. This preserves failure reasons, causes and
earlier-task precedence, but fallback may consume extra launch attempts. Service
and node inspections and standalone name/prefix resolution remain scalar.
The confirming all-clear pass still runs in full, and interrupted cycles publish
no partial detail. Size response and launch budgets for the selected resources;
batching doesn't remove those limits.

These rules apply to every target. `timeout` on capture and wait is a positive
finite total budget for that call, unlike a per-launch CLI timeout; cleanup
can outlast it. `poll_interval` is also positive finite seconds.
`Deadline.detail`, when present, is a payload-free aggregate from the last
complete polling cycle, never a partial cycle or live state at expiry; it's
absent when capture expired or no cycle finished. It holds `captured_services`
plus counts of affected services per reason, each service counted at most once per reason and
reasons allowed to overlap. Swarm counters are `missing_ready_slots`,
`duplicate_slots`, `stale_templates`, `non_running_tasks`, `invalid_task_shape`,
`ongoing_updates`, `missing_ready_nodes`, `duplicate_node_assignments`,
`missing_node_assignments`, and `outside_approved_nodes`. A job wait fills only
`missing_ready_slots` (a ReplicatedJob short of `TotalCompletions`) and
`missing_ready_nodes` (a GlobalJob with an approved node not yet complete), each
counting affected services rather than tasks or nodes. For Compose and standalone
targets the long-running readiness counters are `non_running_containers`,
`restarting_containers`, `paused_containers`, and `unhealthy_containers` (any
reported health that isn't `healthy`, or missing health with a configured
healthcheck). These are pending blockers, including `starting`; the opt-in
terminal `unhealthy` reason follows the eligibility rules above. Restarting and
paused containers have their own counters; `non_running_containers` tests status
alone.
`unfinished_completion_containers` counts affected completion services, not
individual containers or replicas; the other readiness counters exclude
completion services.
`restart_budget_exceeded_containers` counts affected long-running services once
each, bounded by `captured_services`, from the latest complete cycle. For a
standalone target, each captured container is one `captured_services` unit and
each applicable container counter counts those units; all Swarm-only counters
stay zero.

For a `NodeTarget`, each selected node is one `captured_services` unit.
`non_ready_nodes`, `unavailable_nodes`, `non_manager_nodes`, and
`unreachable_managers` count affected nodes once per reason, with overlapping
reasons allowed and each count bounded by that total. Other target counters stay
zero, except `leader_mismatch_managers`: with leadership enabled it is zero for
exactly one observed designated leader, otherwise the number of designated
managers (not the number of leaders). It is zero when disabled and carries no
identities. Interrupted node cycles, including confirming passes, retain only the last
published complete cycle. Node failures have `service_id=None`; deadline detail
contains only integer counts, never node IDs.

A `Failure.cause` or a target's `template` may hold confidential wire data; they
stay out of `repr()`, but don't log them without a confidentiality policy.
Standalone failures have `service_id=None`.
Cancellation or deadline expiry reaps only the local CLI child; it never cancels
or rolls back daemon-side work, including Swarm, Compose, or standalone
containers.

## Optional Application Reconciliation

`pywhales.reconciliation` exports `Controller`, `JournalStore`, operation/lease
types, and explicit inventory/mutation/convergence adapter contracts. This is an
application-owned, one-pass journal workflow (`submit`, `claim`, `reconcile`),
not an automatic Docker scheduler or a wrapper that changes ordinary calls.
The application supplies adapters and a production durable store; the included
`MemoryJournalStore` is a thread-safe, nondurable conformance fake, not
process-crash recovery. Unknown mutation outcomes aren't automatically replayed.
Leases don't fence Docker itself; there is no exactly-once guarantee or built-in
reset/retry/delete workflow. Check installed signatures and the application's
ownership/recovery design before opting in; don't replace failed mutating calls
with blind retries.
The journal's convergence failure vocabulary also accepts `completion_failed`,
`exited`, and `unhealthy`. An adapter observing a target captured with
`fail_on_unhealthy=True` can return that failure; reconciliation projects it to
`FAILED` with convergence `"unhealthy"`, clears any prior deadline detail, and
doesn't replay the mutation. As with `exited`, it retains no container state,
exit code, output, or failure cause.
