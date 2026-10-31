# Offline fleet contributor contract (v1)

This checkout implements proposal01's offline contract, not proposal12's live
harness. It imports no SDK code and starts no child process, socket, daemon,
network probe, provisioner or scheduler. Pydantic is the existing project
dependency. Nothing here changes installed `pywhales` APIs or defaults.

## Commands

Run from the checkout root. `--offline --locked` keeps uv on installed locked
dependencies. The CLI reads only explicitly named UTF-8 JSON files and writes
JSON to stdout; it never dereferences artifact, connection or evidence references.

```bash
uv run --offline --locked python -m scripts.fleet_contract --help
uv run --offline --locked python -m scripts.fleet_contract validate manifest \
  --input scripts/fleet_contract/fixtures/synthetic-runnable.json
uv run --offline --locked python -m scripts.fleet_contract validate manifest \
  --input scripts/fleet_contract/fixtures/draft.json
uv run --offline --locked python -m scripts.fleet_contract validate support \
  --input scripts/fleet_contract/fixtures/support-matrix.json
uv run --offline --locked python -m scripts.fleet_contract schema manifest
uv run --offline --locked python -m scripts.fleet_contract schema support
uv run --offline --locked python -m scripts.fleet_contract schema result
uv run --offline --locked python -m scripts.fleet_contract report \
  --manifest scripts/fleet_contract/fixtures/synthetic-runnable.json \
  --support scripts/fleet_contract/fixtures/support-matrix.json \
  --input scripts/fleet_contract/fixtures/incomplete-result.json
```

The fake harness can export a synthetic result for the same report; see
`scripts/fleet_harness/AGENTS.md`. Replace `incomplete-result.json` with `synthetic-result.json` to inspect a
structurally complete fictional result. Neither command contacts Docker. A
successful validation/report returns 0; invalid input, arguments or unreadable
files return 2. `report` takes a result plus the two explicitly named contracts;
`validate evidence` and `schema evidence` also accept/export a bundled envelope.
Report exit 0 means valid report input, not passing criteria or qualification.

Errors expose only fixed codes (`arguments`, `input_unreadable`, `missing`,
`invalid`) and schema-known field vocabulary/numeric indices. Unknown keys and
union labels become `field`. Errors never echo supplied values, unknown key
names, file paths, Pydantic messages or validator context. Normalized successful
output **does** preserve supplied data. Do not put credentials, private endpoint
URIs, payloads or confidential paths in these files, free-text fields or reports.
Use safe aliases and non-dereferenced references; this is not a secret scanner.

## Schema And Typed Groups

`schema` exports the current models' JSON Schema, including required fields,
extra-key prohibition, finite numeric bounds and version constraints. Schema
version 1 accepts strict JSON types; strings are not converted to counts,
booleans are not counts, and NaN/infinity are rejected. Lists normalize to typed
tuples internally and JSON arrays externally. Unknown fields are rejected at
every nested model. Changed semantics require an explicit schema-version review.

| Group | Fields and units |
| --- | --- |
| Identity | Schema version, draft/runnable state, synthetic/operator-declared provenance, profile, SHA256 manifest revision and workload identity, named owners for every group, artifact destination aliases/access/retention seconds. Image digests must be immutable SHA256 values, not tags. Placeholder draft digests are not real workload identities. |
| Workload | Resident containers, service replicas, in-flight operations and concurrent submissions are distinct integers. Endpoint aliases select effective targets. Arrival/churn rates use operations/second; output uses bytes; log rate uses bytes/second. Each distribution names constant/uniform/empirical shape, finite minimum/maximum and empirical samples. Images carry digest, size bytes and cold/warm/mixed cache state. Operation shares, concurrency, duration seconds and duration/operation-count termination are explicit. |
| Platform | Inventory IDs, private connection-reference aliases, local/SSH/TLS access, CPU cores, memory/disk bytes, storage driver, filesystem and credential-free registry path. Every alias maps to exactly one effective inventory ID; several aliases may deliberately share it. Node count, orchestrator/mode and required features are explicit. An inventory ID is an operator declaration, not discovered daemon identity. |
| Topology | Bridge/overlay, IPv4/IPv6 pools, MTU bytes, DNS expectation, none/host-publish/routing-mesh ingress, operator/tenant trust, mount source references/destinations/read-only state, security controls and enforcement owner. No host or network settings are applied. |
| Ownership | Created/borrowed and persistent/ephemeral asset ledger, retain/remove-owned teardown, protected rollback digests, backup/restore obligations and owner, log/cache/report retention seconds, teardown owner/timeout, expected retained assets and maximum created assets. Borrowed/persistent assets cannot be deleted by this contract. |
| Measurements | Four separate seconds-based boundaries: queue wait over all submissions, selected local CLI over launched CLI processes, end-to-end readiness over readiness attempts, recovery over faults. Each declares start/end events, all-outcome denominator and requested percentile fractions. Resource high-water names encode cores, bytes or counts. Actual per-outcome timing distributions belong to results, not manifest assumptions. |
| Budgets | Operator-supplied latency percentile/seconds, error fraction, maximum recovery seconds and resource high-water/early-stop limits. Poll interval, stability window/observations, readiness definition, sustained duration, maximum duration, error/unknown early-stop limits and cleanup timeout are explicit. Fixture values are fictional, never real SLOs or defaults. |
| Authorization | Operator and approving role, record ID, synthetic/example versus operator-declared purpose, strict disposable declaration, effective endpoint/action allowlists, approved fault scenario aliases, run namespace, ownership marker, isolation/cleanup references, owned-inventory verification surface, stop authority, duration/asset limits and relative authorization validity seconds. No timestamp, signature or reference authenticates real authority here. |

### Authoritative Runtime Invariants

JSON Schema **does not encode cross-field Pydantic validators**. Consumers must
run the Python validator, not use schema-only acceptance as a runnable gate.
Runtime checks reject:

- Missing required groups, unknown aliases, duplicate inventory/alias names and
  authorization targets outside inventory or selected-target coverage.
- Standalone with replicas, non-Swarm overlay/routing mesh, invalid
  orchestrator/mode pairs and non-Swarm multi-node declarations. Recognized
  combinations describe valid structure, **not supported combinations**.
- Distribution bounds/shape/sample contradictions, incompatible units, operation
  shares not summing to one, duplicate actions, impossible concurrency or
  duration/operation-count termination contradictions.
- Disabled/missing IP families/pools, overlapping pools, IPv6 MTU below 1280 or
  MTU above 65535, destructive teardown of borrowed/persistent assets, and a
  created-asset ledger larger than its declared maximum.
- Mismatched measurement populations, duplicate boundaries/metrics/percentiles,
  absent boundaries, omitted budgeted metrics or missing budget percentiles.
- Impossible poll/stability durations, early-stop thresholds beyond maxima,
  workload duration outside sustained/stop/authorization limits, mismatched
  cleanup timeouts or asset limits, and authorization expiring before the run.
- Unallowed actions, omitted cleanup permission, faults without scenario
  declarations and synthetic provenance presented as real authorization.
- Unresolved runnable fields, unowned/dangling/duplicate decision references and
  support promotions without exact versions, operator-declared evidence and a
  maintainer approval reference. References are not authenticated.
- Result counts above declared populations, subpopulations above parent counts,
  duplicate observations and samples larger than their declared population.
- Samples without an integer `submission`, a `submission` outside
  1..`populations.all_submissions`, or two samples for one submission in a boundary.
- Sample reasons contradicting measured/excluded state, stability windows longer
  than elapsed time, and pending decision markers presented as observed versions.

### Drafts And Decisions

A draft can replace each not-yet-selected group (or its `support_row`) with
`{"decision_id":"budgets"}`. Its `decisions` must contain one matching unique ID
and exact field path, owner, rationale and blocking effect (`live_run`,
`qualification`, `legacy_compatibility`). Unresolved matrix versions or legacy
policy choices can use the same marker and indexed paths such as
`rows.0.versions.docker_engine`. Empty arrays/null are not unresolved markers.
Every marker must resolve to a decision and every decision must refer to a marker.

The draft fixture leaves actual operator workload, capacity, targets, authority,
topology, ownership, measurement design and budgets unresolved. The responsible
roles are recorded; they still need named humans before a real run. Its identity
hashes and local draft artifact policy are placeholders, not approved run inputs.
Changing `state` to `runnable` without resolving every marker is rejected.
`runnable` means fully specified configuration, never permission to execute.

### Maintained Support Matrix

`fixtures/support-matrix.json` is the contributor-owned matrix. It names the
configured Linux modern-plugin combination and Windows volume-only smoke path,
plus a clearly fictional example row. Exact unobserved CLI/Engine/kernel/Windows
versions have owned blockers. Python and Linux plugin pins come from
`.github/workflows/python-package.yml` and `scripts/docker-plugin-checksums.txt`;
configured CI is not passing or fleet-capacity evidence. Other combinations,
including Swarm and Compose workloads, have no approval from these rows.

Statuses are `supported`, `experimental`, `untested`. All checked-in rows remain
**untested**: there are no approval/run receipts. No supported or experimental
combination is invented. Such a row requires complete version observations,
maintainer/approval/evidence references, and independent review outside this
tool. Compose/Buildx `null` means not applicable to that bounded row, not unknown;
unknown applicable versions use a decision marker. Versions must be exact,
not `latest`, ranges or implicit runtime probes.
Compose is applicable when the orchestrator is `compose` or `required_features`
contains `compose`; Buildx is applicable when `required_features` contains
`buildx`. Applicable plugin versions cannot be null, including untested rows;
owned decision markers are allowed only in untested rows. Actual observed
versions cannot contain markers and must match the selected row.

`legacy06` records that the deprecated `stack.deploy()` orchestrator compatibility
ends before 0.6.0 and the argument is removed in 0.6.0. This release policy does
not qualify any Docker combination.

## Result And Report Semantics

**Schema-version decision: version 1 does not move for the required `submission`
link.** This is a deliberate choice, not an oversight. The version constant is
shared by the manifest, support matrix and result, and none of the other groups
changed. The checked-in fictional fixtures are updated here. Existing version 1
results without links must be migrated before they can be read.
Fail-closed parsing carries the change: a result without links is rejected by the
strict models (`missing` at `$.latency.N.samples.M.submission`), never reinterpreted.
Link violations report fixed codes with field names and indices only. If results
written before this change ever need to stay readable, version the result then.

Result files bind manifest revision, support row, observed versions,
authorization reference, run namespace and safe endpoint aliases. They retain
population counts, all terminal outcomes, raw per-outcome seconds, exclusion
reasons, resource high-water values, elapsed/stability observations, missing and
excluded evidence flags, isolation/artifact/cleanup references and unexpected
residual assets. `residual_assets` means unexpected assets remaining after owned
cleanup, not the manifest's deliberately protected/borrowed retained assets.

`succeeded`, `failed`, `rejected`, `cancelled`, `unknown` and `timed_out` are
mutually exclusive terminal categories. Timed-out observations are not counted
again as failed/unknown. Their sum must not exceed all submissions. A short sum
is allowed as incomplete evidence and flagged, never success. Every sample carries
a required integer `submission` (1..`populations.all_submissions`, unique within a
boundary), linking a submission's samples across boundaries. The assessment
reconciles those links: a submission whose outcome differs between boundaries is
flagged `submission_outcome_conflict`, and the linked outcomes must reproduce the
aggregate counts or `outcome_samples_mismatch` is flagged. Both are missing
evidence, so a submission cannot fail at queue wait yet succeed at every later
boundary in a complete result. Non-queue samples use their own explicit
launched/readiness/fault populations. Excluded and missing seconds are retained,
not zero-filled or silently removed from the denominator. A genuine measured
zero differs from unavailable `null`.
`excluded` reasons require the exclusion flag; other reasons cannot set it.
`collection_failed` and `unavailable` require null seconds, while `observed`
requires measured seconds. Excluded records may retain measured seconds but
never contribute to measured distributions.

Percentiles use nearest rank over all observed, non-excluded seconds, including
failure timings, alongside expected/recorded/measured/excluded counts and the raw
outcomes. Percentile 0 selects the minimum; 1 selects the maximum. Missing,
excluded, zero-sized required populations, absent metrics/receipts, revision or
environment mismatches, early stopping, incomplete stability collection and
unexpected residual assets prevent completeness. Failed numerical budgets can
have complete evidence but failed criteria. All non-success terminal categories
count in the error-rate denominator; maximum recovery applies to every measured
recovery sample. Omitted observations make criteria `unknown`, not passed.

The report separates three concepts:

1. `schema_valid=true`: the input passed strict models and runtime invariants.
2. `evidence_complete`: the declared record contains the required observations,
   populations and references. This is a content-completeness assertion only.
3. `qualification="unqualified"`, always, with
   `review_required="independent_maintainer_and_operator"`. Neither simulation,
   schema validity, complete evidence, passing budgets nor an approval-reference
   string can grant qualification or establish real authority/capacity.

Synthetic evidence carries `synthetic_non_authorizing`. Unapproved support stays
visible as `support_unapproved`, even for a complete fictional result. Real
qualification requires independent maintainer/operator review of actual approved
versions, authority, isolation, observations, artifacts and cleanup. Offline
parsing cannot authenticate provenance or certify these facts. Live execution and
independently verified cleanup belong to proposal12; no authenticity system is
implemented here.

Completeness also requires the workload's declared termination threshold:
elapsed seconds at least the declared duration, or all submissions at least the
operation-count target. The operation target counts submissions, not successes;
outcomes and budget checks remain separate. Unfinished runs have unknown criteria.
Nonzero fault populations require a positive-share workload fault action,
permission for that action and a declared destructive scenario. The shared
synthetic manifest declares a fictional fault for both result fixtures; this
does not authorize a real fault or live run.

## Focused Checks

```bash
uv run --offline --locked pytest tests/pywhales/test_fleet_contract.py \
  tests/pywhales/test_fleet_report.py tests/pywhales/test_fleet_cli.py \
  tests/pywhales/test_fleet_invariants.py tests/pywhales/test_fleet_harness.py \
  tests/pywhales/test_fleet_harness_cli.py \
  tests/pywhales/test_docker_overhead_benchmark.py \
  tests/pywhales/test_client_config.py tests/pywhales/test_docker_client.py \
  -m 'not docker' -q
uv run --offline --locked ruff check ./
uv run --offline --locked ruff format --check ./
uv run --offline --locked ty check
uv run --offline --locked ty check \
  --config 'src.include = ["scripts/fleet_contract"]' scripts/fleet_contract
```

The explicit ty include matters: repository defaults include SDK/tests/docs, not
`scripts`. Tests deny subprocess/socket/DNS contact while parsing unsafe inputs,
exercise the actual CLI with Docker absent from PATH, and check incomplete and
success-only reports. No test pins this prose. Do not run the destructive full
integration suite or CI daemon provisioning on a workstation for this increment.
