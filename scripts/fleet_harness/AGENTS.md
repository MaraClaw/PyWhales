# Fleet Harness Guide

Fake-only simulation and non-authorizing transport decisions; no README here. Read `../fleet_contract/`'s guide and README before changing declarations.

## Editing Map

| File | Change here |
| --- | --- |
| `models.py` | Resolved manifest, `Step` with arrival/queue-lease/fixed or exponential retry settings, `QueueRetryExponential`, bounded `Plan`, `authorize` preflight, schedule version and deadline helpers |
| `runner.py` | Sequential loop, dispatch of scheduled plans, observation summaries, owned ledger, operation digest, and cleanup orchestration |
| `scheduler.py` | Opt-in overlapping load: event heap, FIFO queue under global and per-action limits, tie ordering, early-stop drain |
| `slots.py` | Slot-to-identity resolution and the asset cap, shared by the sequential and scheduled paths |
| `fake.py` | In-memory clock, operation phases (`begin`/`advance_phase`/`finish`/`lapse`), and exact-identity cleanup |
| `records.py` | Simulation reports, versioned binding evidence and canonical fingerprints, frozen schedule receipts and peaks, population/call accounting, status reasons, withheld connections |
| `timeline.py` | Pure receipt arithmetic: settled peaks, FIFO order, and the fixed `schedule_*` reconciliation codes |
| `export.py` | `export_result` and `ExportRefused`: synthetic, non-authorizing contract evidence from a run report |
| `experiment.py` | Prerecorded comparison and the permanently non-authorizing `Decision` |
| `__main__.py` | Subcommands, shared contract loaders/errors, and JSON/exit boundaries |
| `fixtures/` | Fictional manifest, plan, and no-baseline experiment inputs |

## Execution Contracts

- `RunnableManifest` reruns the contract's Python validators, including cross-group checks. `runner.run` calls `models.authorize`
  before `FakeDriver`: rejection leaves calls, submissions and ownership empty. `opt_in` is required; `provider="real"` rejects.
- Preflight checks selected endpoint aliases and effective targets, declared positive-share actions,
  fault allowlists, and created/ephemeral/remove-owned asset slots. `cleanup` isn't a scripted step.
- `Plan.maximum_samples` bounds accumulated observations. Keep per-operation timeouts and the minimum
  of workload duration, authorization duration/expiry, and early-stop duration, all on the fake clock.
- Preserve failure/unknown, unavailable-resource, resource-threshold, and asset-cap stops. Duration
  completion differs from operation-count completion; cleanup time can't complete an exhausted load.
- Cleanup runs in `finally`, removes only exact acquired live identities within one cleanup budget,
  and reports residuals. Never delete borrowed/persistent assets or infer ownership from a name prefix.
- Overlap is opt-in: all steps need finite nonnegative nondecreasing `arrival_seconds`, one repetition, and no scripted `queue_seconds`.
  Partial offsets reject. No offsets retain sequential execution and digest; scheduled digests also bind offsets.
  Both paths use the same `authorize`; concurrency grants no permission. Old sequential JSON without `schedule` or `arrival_seconds` imports.
- `scheduler.Simulation` admits FIFO under global `workload.concurrent_submissions` and per-action `operation_mix.concurrency`.
  A capped head blocks later actions; `wake` schedules one admission pass only when both head limits allow it, a waiter exists, and no stop/pass is pending.
  Phases, command counts, asset identities, and removals change at fake event time; never shift sequential timestamps to imitate overlap.
- A logical deadline is `min(load deadline, original arrival + operation timeout)`, including every queue attempt; retries never extend it. Arrivals at/beyond the load deadline are excluded; `operation_count` termination slices the steps.
- Scheduled steps alone accept positive finite `queue_lease_seconds` and strict integer `max_queue_attempts >= 1` (includes the first attempt; omitted means one). More than one attempt requires a lease. Unset fields are omitted from reports and operation digests, preserving legacy plans; set fields bind the digest.
- An unlaunched attempt expires at `min(enqueue + lease, logical deadline)`, or at the deadline without a lease. If expiry precedes the deadline and attempts remain, rejoin the FIFO tail immediately unless fixed or exponential backoff is declared; otherwise produce one unlaunched `timed_out` receipt with no command or asset. Launched work never retries.
- Optional `queue_retry_backoff_seconds` is strictly positive and finite. `queue_retry_exponential` instead supplies strictly positive finite `initial_seconds` and `max_seconds`, with max >= initial; after expired attempt `n` its delay is `min(max_seconds, initial_seconds * 2**(n - 1))`. Capped doubling avoids overflow and preserves subnormal values. Both policies require a scheduled lease and `max_queue_attempts > 1`; they are exclusive on one step but may mix across steps. Supplied policies bind the operation digest; unset fields stay omitted.
- Eligibility is lease expiry plus the selected delay. Pending backoff occupies neither queued nor active capacity; eligibility at/beyond the original logical deadline terminates there without another attempt. Exhausting attempts terminates at the final lease expiry, without another backoff. Early stop closes every pending interval immediately, including zero-length intervals when expiry itself triggers the stop.
- Tie order per instant, then submission: phase ends (final frees slot), queue expiries, plan-order original arrivals, retries, admissions. Preserve the `scheduler.py` docstring order; retry FIFO uses attempt enqueue time, not original arrival.
- A terminal event that stops the load (failure/unknown/resource limit, or the asset cap at admission) cancels queued
  and pending-retry work, drops later arrivals, and lets active operations drain through their own deadlines before the `finally` cleanup.
- Same-slot work resolves the latest live identity at admission. A removal commits at its phase end only if that is
  before its deadline and the identity is still live; later starts then fail instead of using a stale resource.
- `Schedule` is frozen: receipts bind actions; `peaks.per_action` is canonical, sorted by action, and includes launched actions only.
  Recount active `[admission, terminal)` and every queued attempt's `[enqueued_seconds, ended_seconds)` span, settled per timestamp; legacy receipts without `attempts` retain `[arrival, admission or unlaunched terminal)`.
- Schedule version follows the whole plan, including rejected plans: implicit v1 without serialized `version` for no backoff, v2 for fixed-only, v3 if any step declares exponential backoff. The separate `Binding.version` stays 1. Delayed receipts include `backoffs` (empty when no lease expired), with half-open `started_seconds`/`ended_seconds` intervals joining consecutive attempts or a pending terminal outcome; other receipts omit them. Legacy v1/v2 serialization and operation digests stay unchanged.
- Leased receipts retain contiguous, non-overlapping attempt lineage, or explicit intervening backoffs, within the lease/deadline/attempt budget, with at most one admission. `Schedule.coherent` and `timeline.lineage`/`is_ordered`/`peak_counts`/`reconcile` independently validate imported alternating histories, fixed or capped exponential delays, original deadlines, terminal events and FIFO order without scheduler replay; unexplained gaps reject. Backoff spans never contribute to queue peaks. Each logical submission still has one terminal outcome and one queue-wait sample measured from original arrival, including backoff time; exported `all_submissions` counts submissions, not retries.
- Pending cancellations establish one stop timestamp: imported unlaunched histories cannot terminate after it, even if relabeled as deadline timeouts. Admitted work may drain afterward; terminal events exactly at stop retain event-order semantics.
- Pydantic validation types are fixed and payload-free: `schedule_order` (numbering/FIFO), `schedule_peaks` (receipt recount,
  including per-action peaks), `schedule_binding` (actions, offsets, repetitions, status, sequential/scheduled),
  `schedule_concurrency` (global/per-action cap), `schedule_times` (sample order/times, window, deadline).
- `RunReport` counts outcomes once per submission, retains failure/unavailable timings, and enforces `provider_calls == command_count + len(cleanup.attempted)` plus status/reason consistency.
- Reports stay simulated/unqualified with zero connections; connection references are withheld even in imported comparisons. Declarations prove no target identity, permission, or isolation.
- New reports carry version 1 binding fingerprints of the complete normalized manifest and the retained sanitized environment. Each hashes UTF-8 stdlib JSON with sorted keys, compact separators, ASCII escaping, finite numbers, included defaults and preserved array order. The manifest hash includes connection references; the environment hash uses `withheld` references. Import verifies the latter; legacy reports without binding remain readable but cannot export or match. Hashes establish content consistency, not authenticity or authorization.
- Bind rejected runs too. Import rejects explicit null, partial/malformed or unknown-version bindings; never manufacture binding while loading legacy reports. Export recomputes the complete manifest fingerprint, so identical revision/workload strings cannot rebind changed policy, budgets, ownership, topology, artifacts, support rows or connection references. Keep the fixed payload-free refusal boundary.
- Comparison requires both reports bound in both domains. A supplied baseline must match the CLI report's workload digest and binding environment digest; an unrelated environment cannot satisfy it. Binding changes neither operation digests nor schedule compatibility, and never enables transport execution.
- `experiment.decide` always selects `no_go` and route `cli`, with zero candidate launches/connections. A real baseline or matched simulation
  can't enable an adapter; Compose/Buildx stay unchanged. Don't introduce Docker, network, or real-backend execution paths.

## CLI Surface

Run from the repository root with `uv run --offline --locked python -m scripts.fleet_harness`:

- `run --manifest M --input PLAN` emits a simulation report.
- `export --manifest M --input REPORT` converts a fake report to a synthetic contract result; rejected reports/non-synthetic manifests are refused.
  Authorization/receipt references stay absent. Feed the result to
  `python -m scripts.fleet_contract report --manifest M --support S --input RESULT`.
- `decide --input EXPERIMENT` emits a no-go decision over prerecorded inputs.
- `schema --kind {manifest,plan,experiment}` exports the selected input schema.

Exit 0 means JSON was produced (including stopped runs/no-go decisions), not qualification; exit 2 means invalid arguments/input or rejection, retaining zero-call counts.
The CLI sanitizes `schedule_*` types to `invalid` plus a schema-known path (for example `$.schedule`), never the internal type, values, unknown keys, file paths, or validator context.

## Focused Checks

Tests under `tests/pywhales/`: `test_fleet_harness.py` covers preflight, stops, retention, cleanup/residuals, accounting, compatibility,
and overlap (simultaneous arrivals, lease retries/exhaustion, original deadlines, no retries after launch, pending-retry cancellation, legacy digests, same-slot lifecycle, drain, injected-failure cleanup, lineage tampering); its `overlap_manifest`,
`overlap_plan` and `at` helpers are reused. `test_fleet_harness_cli.py` covers exits/sanitized errors with Docker absent from `PATH`;
`test_transport_experiment.py` covers comparisons, `test_fleet_report.py` overlapping exports, `test_json_tooling.py` digests/SDK independence.
`test_fleet_binding.py` covers canonicalization, same-revision declaration changes, environment tampering, malformed and legacy imports, export rebinding and baseline mismatches. Preserve process/socket/DNS denial boundaries and Docker-absent CLI checks.
`test_fleet_backoff.py` covers fixed/exponential delays, caps, overflow/subnormal values, mixed policies, rejected-plan versions, ties, capacity exclusion, deadline clipping, exhaustion, stop/drain, independent histories, tampered imports and legacy digests. `test_fleet_binding.py` also checks changed exponential-policy digest comparisons with binding v1; backoff export and CLI imports live beside immediate-retry regressions.
Root ty omits scripts. Include both packages explicitly for a scoped check from the repository root:

```bash
uv run --offline --locked ty check --config 'src.include = ["scripts/fleet_contract", "scripts/fleet_harness"]' scripts/fleet_contract scripts/fleet_harness
```
