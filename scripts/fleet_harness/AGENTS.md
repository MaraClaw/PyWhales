# Fleet Harness Guide

Fake-only simulation and non-authorizing transport decisions; this package has no README.
It builds on `../fleet_contract/`; read that package's guide and README before changing declarations.

## Editing Map

| File | Change here |
| --- | --- |
| `models.py` | Resolved manifest, `Step` with its optional `arrival_seconds`, bounded `Plan`, `authorize` preflight, load deadline and termination helpers |
| `runner.py` | Sequential loop, dispatch of scheduled plans, observation summaries, owned ledger, operation digest, and cleanup orchestration |
| `scheduler.py` | Opt-in overlapping load: event heap, FIFO queue under the global limit, tie ordering, early-stop drain |
| `slots.py` | Slot-to-identity resolution and the asset cap, shared by the sequential and scheduled paths |
| `fake.py` | In-memory clock, operation phases (`begin`/`advance_phase`/`finish`/`lapse`), and exact-identity cleanup |
| `records.py` | Simulation reports, frozen schedule receipts and peaks, population/call accounting, status reasons, withheld connections |
| `timeline.py` | Pure receipt arithmetic: settled peaks, FIFO order, and the fixed `schedule_*` reconciliation codes |
| `export.py` | `export_result` and `ExportRefused`: synthetic, non-authorizing contract evidence from a run report |
| `experiment.py` | Prerecorded comparison and the permanently non-authorizing `Decision` |
| `__main__.py` | Subcommands, shared contract loaders/errors, and JSON/exit boundaries |
| `fixtures/` | Fictional manifest, plan, and no-baseline experiment inputs |

## Execution Contracts

- `RunnableManifest` reruns the contract's Python validators. Don't narrow away cross-group checks.
- `runner.run` calls `models.authorize` before constructing `FakeDriver`. Rejection must leave
  provider calls, submissions, and ownership empty. `opt_in` is required; `provider="real"` is rejected.
- Preflight checks selected endpoint aliases and effective targets, declared positive-share actions,
  fault allowlists, and created/ephemeral/remove-owned asset slots. `cleanup` isn't a scripted step.
- `Plan.maximum_samples` bounds accumulated observations. Keep per-operation timeouts and the minimum
  of workload duration, authorization duration/expiry, and early-stop duration, all on the fake clock.
- Preserve failure/unknown, unavailable-resource, resource-threshold, and asset-cap stops. Duration
  completion differs from operation-count completion; cleanup time can't complete an exhausted load.
- Cleanup runs in `finally`, removes only exact acquired live identities within one cleanup budget,
  and reports residuals. Never delete borrowed/persistent assets or infer ownership from a name prefix.
- Overlap is opt-in per plan. Every step carries a finite nonnegative `arrival_seconds` in nondecreasing order,
  with one repetition and no scripted `queue_seconds`; a partial set is rejected. Plans without offsets keep the
  sequential loop, reports, and operation digest unchanged. A scheduled digest also binds the offsets. Declared
  concurrency never grants permission: `authorize` is the same for both kinds.
- `scheduler.Simulation` admits arrived submissions FIFO up to `workload.concurrent_submissions` (global). Per-action
  `operation_mix.concurrency` isn't simulated. Phases, command counts, asset identities, and removals change at event
  time on the fake clock; never shift sequential timestamps to imitate overlap.
- A deadline is `min(load deadline, arrival + operation timeout)`, so queue time counts. Queued expiry yields an
  unlaunched `timed_out` receipt with no command or asset. Arrivals at or beyond the load deadline are excluded, and
  `operation_count` termination slices the steps.
- Tie order at one instant, then by submission: phase ends (a final one frees its slot), queue expiries, arrivals in
  plan order, then admissions. Keep that order in the `scheduler.py` docstring; reordering changes outcomes.
- A terminal event that stops the load (failure/unknown/resource limit, or the asset cap at admission) cancels queued
  work, drops later arrivals, and lets active operations drain through their own deadlines before the `finally` cleanup.
- Same-slot work resolves the latest live identity at admission. A removal commits at its phase end only if that is
  before its deadline and the identity is still live; later starts then fail instead of using a stale resource.
- `Schedule` (receipts and settled peaks) is frozen and validated on import: `schedule_order` (numbering, FIFO),
  `schedule_peaks` (recount from receipts), `schedule_binding` (offsets, repetitions, status, sequential vs scheduled),
  `schedule_concurrency` (peak above the limit), `schedule_times` (sample order and times, window, deadline).
  Reports without `schedule` import as sequential. Keep these codes fixed and free of input values.
- `RunReport` counts terminal outcomes once per submission and retains failure/unavailable timings.
  Keep `provider_calls == command_count + len(cleanup.attempted)` and status/reason consistency.
- Reports remain simulated and unqualified, with zero connections. Connection references are withheld
  even in imported comparisons; declarations don't prove target identity, permission, or isolation.
- `experiment.decide` always selects `no_go` and route `cli`, with zero candidate launches and connections.
  A declared real baseline or matched simulation can't enable an adapter; Compose/Buildx stay unchanged.
  Don't introduce Docker, network, or real-backend execution paths.

## CLI Surface

Run from the repository root with `uv run --offline --locked python -m scripts.fleet_harness`:

- `run --manifest M --input PLAN` emits a simulation report.
- `export --manifest M --input REPORT` converts a fake run report to a synthetic
  contract result. Rejected reports and non-synthetic manifests are refused;
  authorization and receipt references remain absent. Feed the result to
  `python -m scripts.fleet_contract report --manifest M --support S --input RESULT`.
- `decide --input EXPERIMENT` emits a no-go decision over prerecorded inputs.
- `schema --kind {manifest,plan,experiment}` exports the selected input schema.

Exit 0 means JSON was produced, including stopped runs and no-go decisions, not qualification.
Exit 2 means invalid arguments/input or a rejected run; rejection receipts retain zero-call counts.

## Focused Checks

Tests under repository-root `tests/pywhales/`: `test_fleet_harness.py` covers preflight-before-driver,
stop edges, sample retention, exact cleanup/residuals, imported-report accounting, and the overlap contracts
(simultaneous arrivals, queue expiry and ties, same-slot lifecycle, early-stop drain, injected-failure cleanup, tampered
schedules) through the `overlap_manifest`, `overlap_plan` and `at` helpers that the other files reuse;
`test_fleet_harness_cli.py` checks actual CLI exits/errors with Docker absent from `PATH`;
`test_transport_experiment.py` checks semantic comparisons without enabling candidate execution;
`test_fleet_report.py` classifies exported overlapping runs; `test_json_tooling.py` pins digests and SDK independence.
Root ty omits scripts. Include both packages explicitly for a scoped check from the repository root:

```bash
uv run --offline --locked ty check --config 'src.include = ["scripts/fleet_contract", "scripts/fleet_harness"]' scripts/fleet_contract scripts/fleet_harness
```
