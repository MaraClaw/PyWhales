# Reconciliation

## Local Navigation

- `_intent.py`: hashed desired state, operation identity. `_journal.py`: entry, progress, lease values.
  `_store.py`: `JournalStore`, `StoreSemantics`, `MemoryJournalStore`. `_adapters.py`: adapter protocols, evidence.
  `_controller.py`: `Controller`. `_errors.py`: payload-free errors. `__init__.py` is the export list.
- User prose lives in `docs/template/user_guide/reconciliation.md`; edit the template, not generated output.

## Intent And Journal Values

- The journal stores references, never payloads. `DesiredDescription` holds an `InputReference` (SHA-256 hex of
  externally approved input), owner, resource class, target, and `SecretReference` name/version pairs. Don't add
  commands, environment, free-form JSON, task templates, or secret values. Secret references sort by name, reject
  duplicate names, and don't affect the hash by order.
- `desired_hash` is `pywhales.desired.v1:sha256:<hex>` over sorted-key canonical JSON. Changing hashed fields or
  their encoding needs a new `HASH_VERSION`, because stored hashes are compared against recomputed ones.
- Owner, operation, and endpoint alias cap at 64 characters so they round-trip through storage-v1 labels; other
  metadata allows 128. Oversized input fails before journal acceptance.

## Store Contract

- Every write checks version and generation together. `claim` bumps both, even for a repeated worker token; CAS,
  `renew` and release need an unexpired matching lease on the store's own clock. A live lease can't be claimed early.
- `renew(expected, LeaseRequest)` needs the exact entry and the lease's own worker. It sets expiry to store time plus
  TTL (not old expiry plus TTL) and bumps only `version`: generation and progress stay, and nothing new is journaled.
  The old entry is then stale, so callers must carry the returned one. Expired, released, foreign or stale entries
  raise `StoreConflict`; a later `claim` still bumps both counters.
- Attempts never decrease and a progressed entry never returns to `SUBMITTED`. Operation tokens are unique across
  the store, not per endpoint; conflicting intent or a rebound endpoint raises `IdentityConflict`.
- Keep `JournalEntry` tombstones while identities can be reused; deleting one loses deduplication and fencing history.
- Failures surface as payload-free `StoreUnavailable`, even when a commit may have happened. Suppress backend
  exceptions with `from None`, and never assume a failed write rolled back.
- `MemoryJournalStore` is non-durable, single-process, and serialized by one lock. It tests control flow, not crash
  recovery. No durable backend ships; an adapter must declare `StoreSemantics` and keep versions across restarts.

## Controller Pass

- One pass per `reconcile` call: no scheduler, no automatic mutation retry. Use `submit` (create or read, no Docker),
  then `claim`, then `reconcile`. Every pass inventories first, even after `SUCCEEDED`, so schedule periodic passes.
- Mutation needs `SUBMITTED`, zero attempts, and `ABSENT` evidence with no resources. The controller commits
  `Progress(UNKNOWN, 1)` and keeps the lease before `apply`. `UNKNOWN` also covers crashes and lost responses, so
  `AmbiguousMutation`, `DockerException`, `OSError`, or a store outage after launch leave it set.
- After any attempt, `ABSENT` or `INDETERMINATE` evidence stays `UNKNOWN` for an operator to resolve. Never replay.
  `CONFLICT` raises `IdentityConflict`. Preexisting `APPLIED` resources don't unlock mutation.
- `APPLIED` evidence must be non-empty, unique by kind and identity, and exactly owned: endpoint, owner, class, and
  operation match the spec. `OBSERVING` commits before `observe`. `Success` and `Failure` finish the attempt.
  `Deadline` and `Cancelled` stay `OBSERVING`; `deadline_detail` is the exact integer-only `DeadlineDetail`, valid
  only for convergence `"deadline"`, and a later outcome clears it.
- `ConvergenceSummary` includes `completion_failed` and opt-in long-running `exited`/`unhealthy`: project each to `FAILED` with the closed reason and no deadline detail. Keep its failure literals aligned with convergence `Failure.reason`. Never journal container state, health payloads, exit codes, output or a failure cause; [convergence semantics](../convergence/AGENTS.md) stay there.
- `_context` verifies the lease before each adapter call, and adapters call `check_current` before every launch.
  Docker can't enforce this, so a pause after a check still races a lease transfer.
- Only the convergence adapter's context can `renew_lease(ttl)`; inventory and mutation contexts raise `RuntimeError`
  and keep their plain lease checks. Adapters renew between short `convergence.wait` slices inside one outer
  `observation()`, so a wedged observer still expires. No thread or timer renews, and no heartbeat belongs here.
- `_observe` holds the latest renewed entry in `_ObservedLease`: `check_current`, further renewals, the outcome commit
  and release all use it, so a renewed pass commits exactly what a short one would. A renewal `StoreConflict` or
  `StoreUnavailable` halts that object for good (a failed renewal may have committed, never assume rollback): later
  checks and the post-observe commit re-raise it, even if the adapter swallowed it, with no outcome commit.
- Observation errors become `AdapterUnavailable` `from None`. Store conflicts and outages halt the pass with no
  further adapter action. `AdmittedReconciliation` defers the whole pass; adapters must not nest admission. A spec for
  another endpoint fails in `submit` and `claim`; an admission owner for another endpoint fails at construction.

## Adapters

- Adapters are explicit opt-in, bound to one `EndpointIdentity`, with no hidden retries. `ownership_labels` yields
  `io.pywhales.storage.v1.*` metadata that `pywhales.storage.OwnershipLabels` must keep decoding. Labels are
  metadata, never deletion authorization.
- `APPLIED` needs the complete desired operation with verified labels, not names. `ABSENT` never proves an earlier
  in-flight action can't appear; use `INDETERMINATE` for partial, stale, or ambiguous evidence. Map possibly
  effective mutation failures to `AmbiguousMutation`; a CLI timeout isn't a rollback.

## Related Tests

- `tests/pywhales/test_reconciliation.py` and `test_reconciliation_store.py`: controller flows, ownership, CAS, leases,
  and injected-clock renewal (`Clock` lives in `test_reconciliation.py`); no sleeps.
- `tests/pywhales/test_reconciliation_recovery.py`: store faults, lease expiry, admission, no-replay recovery, renewal
  outages and takeover after an observer stops renewing.
- `tests/pywhales/test_reconciliation_ownership.py`: 64-character bounds round-trip through storage labels.
- `test_reconciliation.py::test_real_cli_completion_failure_is_journaled_as_a_closed_reason` guards completion failure projection without raw payloads.
- `test_reconciliation.py` also drives fake-CLI `fail_on_exit` observation through the controller, verifying `FAILED`/`exited`, cleared deadline detail, and no exit-code/output payload.
- `test_real_cli_unhealthy_failure_clears_deadline_without_replay` in that module covers `fail_on_unhealthy`, closed `FAILED`/`unhealthy`, cleared deadline detail, payload exclusion and no repeated mutation.
