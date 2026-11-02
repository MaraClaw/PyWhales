# Service, stack, node and container convergence

`pywhales.convergence` is an opt-in, read-only observer. Deploy, service update,
and rollback retain their existing behavior and do not invoke it. Capture each
exact immutable service ID after submission; the caller must know the complete,
fixed membership of a stack. Never infer it from an empty or changing discovery.

```python
from threading import Event

from pywhales import DockerClient
from pywhales.convergence import ServiceTarget, StackTarget, capture_service, wait
from pywhales.inventory import InspectionPolicy

client = DockerClient()
policy = InspectionPolicy(
    max_references=32, max_argv_bytes=65536, max_response_bytes=1048576
)
cancel = Event()
first = capture_service(
    client, "full-service-id-1", policy=policy, timeout=10, cancel=cancel
)
second = capture_service(
    client, "full-service-id-2", policy=policy, timeout=10, cancel=cancel
)
if isinstance(first, ServiceTarget) and isinstance(second, ServiceTarget):
    target = StackTarget(services=(first, second))
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1, cancel=cancel)
```

Capture returns `ServiceTarget`, `Failure`, `Deadline`, or `Cancelled`. The waiter
returns `Success`, `Failure`, `Deadline`, or `Cancelled`. A stack target must be
nonempty and contain distinct service IDs. Targets retain the originating mutable
client configuration; do not change its endpoint while using them.

Each target records the service version and a fingerprint of its complete desired
task template and mode. Task specs are compared exactly, including unknown wire
fields, except that omitted `Runtime` and `Runtime: "container"` are equivalent.
The task object's own version is not a service-template generation. Replicated
long-running container services require an explicit nonnegative replica count.
Global long-running container services require `approved_node_ids` at capture: a
nonempty tuple of distinct, nonempty immutable node ID strings. For example:

```python
agent = capture_service(
    client,
    "full-service-id",
    policy=policy,
    timeout=10,
    approved_node_ids=("full-node-id-1", "full-node-id-2"),
)
```

The tuple is the fixed caller-approved membership, not a discovery of eligible
nodes. It is rejected for Replicated services. Job, unknown and malformed modes
fail closed. A zero-replica service succeeds only with no desired-running tasks
and a stable generation. A stack may contain both modes.

Only current desired-running tasks count; historical shutdown tasks do not. Each
required slot must contain exactly one matching running task. Missing, stale,
duplicate or ambiguous evidence remains pending. `updating` remains pending;
absent update status or `completed` may succeed. `paused`, `failed`, and all
rollback states are terminal `update_failed` failures, including a completed
rollback. Unknown update states fail as malformed. Changed service versions or
template fingerprints return `superseded`. Inspection and transport errors return
`observation` failures with their original cause. Snapshot collection is not an
atomic Swarm transaction: the final generation recheck reduces, but cannot remove,
concurrent-change races.

For Global services every approved node must have exactly one desired-running,
matching, actually running task. Missing or unassigned nodes, duplicate assignments,
and tasks assigned outside the approved set remain pending. Deadline detail counts
affected services, not nodes or tasks: `missing_ready_nodes`,
`duplicate_node_assignments`, `missing_node_assignments`, and
`outside_approved_nodes` apply only to Global services; slot counts apply only to
Replicated services. The detail contains no node IDs or templates and describes
the latest complete observation cycle, not necessarily the state at expiry.

The policy limits each CLI invocation; task inspections use one reference at a time.
Capture and wait each start their own positive finite monotonic timeout. Cancellation
or deadline terminates and reaps an owned local CLI child; cleanup can outlast the
deadline and does not cancel remote daemon work. No mutation is retried or rolled
back. Running Swarm tasks are not evidence of application health or readiness.

The target's `template` and `Failure.cause`, including exception raw attributes,
may contain confidential service settings, command arguments, and output. Do not
log or expose them without an appropriate confidentiality policy. The template is
excluded from target representations and the cause from failure representations.

## Swarm nodes and designated leadership

`capture_nodes` fixes selected node IDs and an explicit subset of manager IDs;
it never discovers other managers. Each node must be ready and active, and each
designated manager must retain its role and be reachable. For a maintenance step
that also needs leadership evidence, opt in:

```python
from pywhales.convergence import NodeTarget, capture_nodes, wait

target = capture_nodes(
    client,
    ("node-id-1", "node-id-2"),
    manager_ids=("node-id-1", "node-id-2"),
    require_leader=True,
    policy=policy,
    timeout=10,
)
if isinstance(target, NodeTarget):
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1)
```

The flag defaults to false; enabling it requires a nonempty manager subset. Both
complete clear passes must observe exactly one designated leader, but it may be a
different manager in each pass. A missing `Leader` field in a present manager
status means false; an explicit non-boolean leader value on a designated node that
still reports manager role and status returns a cause-free `malformed` failure.
Missing manager status or demotion remains pending. Unselected and undesignated
nodes cannot satisfy or invalidate the leader count.

On expiry, `Deadline.detail.leader_mismatch_managers` is zero when the condition
is met or disabled, and otherwise the number of designated managers. It retains no
identities or inspect payloads and describes the latest complete cycle. Inspections
are sequential: this is neither a global election claim nor a guarantee of
continuous leadership. Capture and waiting do not change the cluster.

## Standalone containers

Use `capture_containers` for containers started outside Compose. Names and ID
prefixes are resolved at capture to fixed full IDs; Compose labels aren't needed.

```python
from pywhales.convergence import ContainerTarget, capture_containers, wait

target = capture_containers(
    client,
    ("database", "migration"),
    completion=("migration",),
    max_restarts=0,
    policy=policy,
    timeout=10,
)
if isinstance(target, ContainerTarget):
    outcome = wait(target, policy=policy, timeout=120, poll_interval=1)
```

References must be a nonempty tuple of distinct nonempty strings, and completion
references must be a subset. Two references resolving to the same container fail
capture. Long-running containers must be running, neither restarting nor paused,
and healthy if configured with a healthcheck. Completion containers must exit zero
without restarting; a nonzero exit returns `completion_failed`. An optional
nonnegative integer `max_restarts` limits long-running containers, as with Compose.

Success requires a second all-clear observation. A captured container disappearing
or being replaced returns `superseded`; capture-time missing references return an
`observation` failure. Deadline details describe the last complete cycle and count
each captured container as one unit in `captured_services`. Capture and wait only
observe Docker state; cancellation stops and reaps the local CLI child.

## Compose restart budgets

For Compose deployments, `capture_compose(client, services, policy=policy,
timeout=10, max_restarts=0)` freezes the selected containers, config hashes and
restart budget in a `ComposeTarget`; pass that target to `wait()` with its own
timeout and polling interval. `max_restarts=None` keeps the existing behavior.
An enabled budget accepts a nonnegative integer, never a boolean, and validates
before any CLI launch.

Each selected long-running container must report a nonnegative integer top-level
`RestartCount` at capture and every observation. Missing or malformed values yield
cause-free `Failure(reason="malformed")`. A healthy running container above budget
keeps its service unsettled; a later within-budget observation can clear that
blocker. Services designated with `completion_services` retain their exit-code
rules and ignore restart counts. Membership and config hashes must stay fixed,
and success still requires the final observation recheck.

On expiry, `Deadline.detail.restart_budget_exceeded_containers` counts affected
services, once per service, from the latest complete cycle. It contains no
container names, IDs or inspect payloads. Cancellation and timeout stop only the
local CLI process, without changing Docker state.
