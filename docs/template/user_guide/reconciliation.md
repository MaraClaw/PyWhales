# Optional Reconciliation Journal

`pywhales.reconciliation` adds an opt-in journal protocol and a one-pass
`Controller`. Ordinary `DockerClient` calls are unchanged. This is not a Docker
transaction, a background scheduler, a resource deletion policy, or an exactly-once
remote executor.

Only `MemoryJournalStore` is provided. It is a transactional, thread-safe,
**NONdurable conformance fake**, not a production backend. Restarting a controller
against the same object demonstrates recovery decisions, not recovery after a
process crash. Durable operation requires an application-supplied `JournalStore`
with explicitly declared and independently tested deployment semantics. No file,
SQLite, distributed database, or managed backend is qualified by this package.

## Intent And Identity

An operation token is unique within its store and permanently binds an endpoint
alias and exact desired description. Reusing the token with different endpoint,
owner, classification, target, approved input, or secret references raises
`IdentityConflict` before any mutation. Endpoint aliases are operator-controlled;
they must consistently identify the intended Docker endpoint, not credentials or
connection strings. They do not prove that two processes use the same daemon.

`DesiredDescription` contains bounded public metadata, an `InputReference` holding
a lowercase SHA-256 digest of externally approved immutable input, and immutable
`SecretReference` aliases/versions. Its `desired_hash` is
`pywhales.desired.v1:sha256:<digest>`, computed from sorted, compact canonical JSON
including the schema version. Secret-reference ordering is immaterial.

Keep approved input outside the journal. The trusted mutation adapter resolves it,
verifies the digest and approval, and resolves secret values only at execution.
Do not put plaintext secrets, commands, environment dictionaries, Compose files,
task templates, arbitrary JSON, raw inspect responses, credentials, or backend
exceptions in journal metadata. Do not hash naked low-entropy secrets as input.
Token validation limits syntax, not confidentiality: aliases must themselves be
non-confidential. Python type annotations are not an untrusted JSON parser;
applications accepting external requests must parse them at their own boundary.

```python
from pywhales.admission import EndpointIdentity
from pywhales.reconciliation import (
    DesiredDescription,
    InputReference,
    OperationKey,
    OperationSpec,
    SecretReference,
)

intent = OperationSpec(
    OperationKey(EndpointIdentity("manager-a"), "deploy-42"),
    DesiredDescription(
        approved_input=InputReference("a" * 64),
        owner="team-a",
        resource_class="ephemeral",
        target="web",
        secrets=(SecretReference("database-password", "version-7"),),
    ),
)
```

## Explicit Application Adapters

Supply `ReconciliationAdapters(endpoint, inventory, mutation, convergence)`. These
are narrow public protocols, not imports of an unfinished storage implementation.
All adapters are bound to the declared endpoint and must honor
`ActionContext.check_current()` immediately before **every SDK launch**, including
preflights, repeated observations and each action of a multi-action workflow.
They must not hide mutation retries or nest admission requests.

`InventoryAdapter.inventory()` returns an `InventoryEvidence` projection:

- `APPLIED`: complete approved desired state is observed, with exact immutable
  resource identities and matching ownership metadata.
- `ABSENT`: fresh authoritative observation finds no desired resources. This does
  **not** prove an earlier in-flight mutation cannot materialize later.
- `INDETERMINATE`: incomplete, stale, partial, unavailable-to-classify or ambiguous
  evidence cannot authorize mutation.
- `CONFLICT`: identity, approved input or ownership contradicts the operation.

Use public `capture_inventory()` for known immutable resource handles with an
explicit `InspectionPolicy`; discovery of resources after a lost create response
is application-specific. Compare actual labels and approved desired input, not
names alone. `ResourceIdentity` persists only kind, immutable identity and public
ownership fields, never `InventorySnapshot.records[*].payload`.

`ownership_labels(intent)` supplies the agreed immutable metadata keys:
`io.pywhales.storage.v1.endpoint`, `.owner`, `.class`, and `.operation`.
Resource classifications are `borrowed`, `persistent`, `ephemeral`, and
`rollback-protected`. Mutation adapters apply these labels at creation; inventory
adapters verify them from resources. The controller rejects conflicting ownership
and duplicate resource identities. Labels identify ownership, not authorization
to delete. Existing approval records must be created if absent or read for the
exact identity and observationally rechecked; this package never deletes them or
implicitly grants destructive approval.

`MutationAdapter.apply()` performs the approved action once. Translate potentially
effective failures to `AmbiguousMutation` with `from None`. A normal CLI return is
not a convergence result. Docker errors and OS errors at this boundary are also
treated conservatively as unknown.

`ConvergenceAdapter.observe()` is separate and returns the actual public
`pywhales.convergence.Outcome`: `Success`, `Failure`, `Deadline`, or `Cancelled`.
For services/stacks, capture the exact approved generation and fixed membership,
then call public `convergence.wait()` with explicit inspection, deadline and
polling bounds. Do not recapture a newer generation as the desired one or
reimplement readiness inside inventory. The journal stores only the outcome
category or bounded `Failure.reason`, not raw state, target templates or causes.
For container captures, the opt-in `fail_on_exit` and `fail_on_unhealthy` policies
produce closed `exited` and `unhealthy` reasons. Either projects to `FAILED`, clears
deadline detail and stores no exit or health payload; it never authorizes a retry.

## Aggregate Observation Budgets

`InspectionPolicy` optionally accepts `max_total_references` and
`max_total_response_bytes`, positive integers that default to `None`. The reference
limit applies independently to each inventory call and counts every occurrence,
including duplicates, before executable lookup. Bulk reload still deduplicates
repeated handles before inspection.

The response limit counts captured raw stdout and stderr bytes read across commands in an
observation scope, including failed commands and successive convergence wait
cycles. Nested scopes charge each enclosing budget; exiting a child does not refund
bytes, and an unrestricted child cannot relax its parent. Reusing a policy in a new
root scope starts fresh. Existing per-command bounds still apply. Exhaustion raises
`ObservationLimitError` and stops the local CLI; it does not cancel daemon-side
work. The byte limit is not a process-memory cap. Observation contexts do not
automatically propagate into arbitrary threads.

`max_total_stream_bytes` is a separate optional positive-integer budget for stream
and archive stdout/stderr reads. Each enclosing scope that sets it is charged;
captured responses do not consume it. Overflow raises `ObservationLimitError`
with reason `total_stream` after the local CLI is stopped and reaped. Archive
watchdog drains and the stderr read used to select a Docker exception are uncharged.

## Driving Recovery

```python
from pywhales.reconciliation import Controller, LeaseRequest


# journal: an application-supplied JournalStore, or MemoryJournalStore for tests.
# adapters: application-supplied ReconciliationAdapters for the exact endpoint.
def reconcile_once(journal, adapters, intent):
    controller = Controller(journal, adapters)
    existing = controller.submit(intent)
    claimed = controller.claim(existing, LeaseRequest("worker-a", ttl=30))
    return controller.reconcile(claimed)
```

The application schedules `reconcile_once` periodically as well as on event hints.
Every pass inventories, even for a previously successful operation. Events are
non-durable wakeups, never the source of truth. A missed completion event is repaired
when the next periodic inventory leads to explicit convergence observation.

A fresh submission can mutate only after authoritative `ABSENT` evidence. Before
launch, a CAS durably records `UNKNOWN` with an incremented attempt count. A crash
between that write and launch is deliberately conservative: recovery cannot tell
whether a launch happened. A timeout or lost response remains `UNKNOWN`; a later
worker **always inventories before deciding**. Complete owned desired state goes
to observation without repeating the mutation. Absent or partial evidence after
an attempt remains unknown and never authorizes automatic replay. Operations
previously observed as applied also cannot become fresh submissions merely
because resources subsequently disappear.

`SUCCEEDED` and `FAILED` describe the latest explicit observation, not immutable
remote facts. `Deadline` and `Cancelled` remain `OBSERVING`. An unknown/failed
operation requires an operator-defined resolution procedure if inventory cannot
settle it. This API intentionally has no reset/retry/delete operation; do not
choose a new identity to bypass unresolved work without establishing that the old
action cannot still take effect.

## Store And Lease Contract

Every adapter declares `StoreSemantics`: actual durability, transaction boundary,
authoritative clock/skew assumptions, retention/tombstones and recovery envelope.
Durable adapters must retain immutable intent, version, generation and the
prelaunch unknown marker through restart, backup/restore and failover within that
declared envelope. Never silently purge identities while a delayed remote action
could still complete. A fresh store is not recovery of a discarded journal.

`create_if_absent`, `read`, `claim`, `verify`, and `compare_and_swap` must be
linearizable across all participating controllers. Creation preserves exact
identity or raises `IdentityConflict`. Claim requires the expected version and
generation and an absent/expired lease, then increments **both**. Every subsequent
write checks exact current version, generation, worker and unexpired lease under
store-authoritative time and increments version. Release removes the lease;
reclaim always advances generation, including reuse of the same worker name.
Active leases cannot be stolen or implicitly renewed. Choose TTLs and observation
bounds appropriately; an expired worker must reacquire from a fresh read.

`StoreConflict` is a failed conditional operation, distinct from recoverable
`StoreUnavailable`. On store outage, stop new decisions and launches. A failed
commit acknowledgement may conceal a successful commit: recover by reading and
inventorying, not by assuming rollback. Adapter boundary errors must be fixed,
payload-free types with backend chaining suppressed (`raise StoreUnavailable()
from None`). Observation failures use `AdapterUnavailable`, not a successful empty
inventory. Existing known resource identities are retained while evidence is
incomplete or resources disappear.

`ActionContext` rechecks the current journal lease before actions. An old worker
cannot start a new checked launch after takeover. **Docker cannot enforce the
journal's fencing generation.** A worker can pause after its last successful
check, lose the lease, then resume a remote call. Already in-flight work also
continues. This check-to-launch race is not eliminated by CAS or a durable store;
inventory-first recovery and withholding ambiguous retries limit duplicate
decisions, not all remote duplication. Multi-action adapters must not treat one
check as authorization for the whole workflow.

Optional `AdmittedReconciliation(owner, pressure, queue_timeout)` wraps the entire
deferred pass with existing `AdmissionOwner.request(...).call(...)`. Its endpoint
must match the adapters. The lease is rechecked **after** admission grants the
permit, so queue delay cannot authorize expired work. Admission remains a local
pressure boundary, not a distributed lease or remote fence.

## Conformance And Limits

The daemon-free tests cover concurrent create, immutable identity conflicts,
conditional versions, monotonic lease transfer, stale workers, queue expiry,
prelaunch and post-launch outages, ambiguous commit acknowledgements, lost remote
responses, periodic missed-event repair and public convergence outcome projection.
Run them with `uv run --locked pytest tests/pywhales/test_reconciliation*.py`.
Application durable adapters additionally need real process-crash, failover,
multi-process contention, clock/skew, commit-ambiguity, retention and restore
tests against their actual backend. Passing the in-memory tests is not evidence
of backend durability or a production support claim.
