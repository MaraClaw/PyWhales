# Fleet Harness Guide

Fake-only simulation and non-authorizing transport decisions; this package has no README.
It builds on `../fleet_contract/`; read that package's guide and README before changing declarations.

## Editing Map

| File | Change here |
| --- | --- |
| `models.py` | Resolved manifest, `Step`, bounded `Plan`, and `authorize` preflight |
| `runner.py` | Stop ordering, observation summaries, owned ledger, and cleanup orchestration |
| `fake.py` | In-memory clock, scripted operations, and exact-identity cleanup |
| `records.py` | Simulation reports, population/call accounting, status reasons, withheld connections |
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
stop edges, sample retention, exact cleanup/residuals, and imported-report accounting;
`test_fleet_harness_cli.py` checks actual CLI exits/errors with Docker absent from `PATH`;
`test_transport_experiment.py` checks semantic comparisons without enabling candidate execution.
Root ty omits scripts. Include both packages explicitly for a scoped check from the repository root:

```bash
uv run --offline --locked ty check --config 'src.include = ["scripts/fleet_contract", "scripts/fleet_harness"]' scripts/fleet_contract scripts/fleet_harness
```
