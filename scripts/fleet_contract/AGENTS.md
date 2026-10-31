# Fleet Contract Guide

Read `README.md` first. It owns commands, units, runtime invariants, and report semantics.
This guide maps edits; `../AGENTS.md` covers the surrounding contributor tooling.

## Editing Map

| File | Change here |
| --- | --- |
| `common.py` | Strict frozen `Contract`, finite primitives, `Pending` and owned `Decision` references |
| `workload.py` | Workload distributions, operation mix, termination, and measurement declarations |
| `platform.py` | Endpoint aliases, topology, and asset ownership validators |
| `safety.py` | Budget, early-stop, and authorization declarations |
| `manifest.py` | Cross-group coherence and resolved runnable-state gate |
| `support.py` | Exact-version applicability, support promotion, and matrix decisions |
| `results.py` | Outcome populations, required sample `submission` links, sample reasons, and result-local consistency |
| `assessment.py` | Measurement accounting, per-submission outcome reconciliation, distributions, and numerical budget assessment |
| `report.py` | `Evidence` binding and completeness classification, separate from qualification |
| `__main__.py` | `SafeParser`, explicit-file loading, schema export, and closed error rendering |

## Boundaries To Preserve

- Schema version 1 forbids extension keys and coercion. Keep finite bounds and frozen models;
  changes to accepted meaning need a schema-version review, not just a regenerated schema.
  The README records why the required sample `submission` link kept version 1.
- JSON Schema doesn't encode cross-field validators. Keep Python validation authoritative;
  add local checks to their owning model and cross-group checks to `Manifest.coherent`.
- `Pending` markers aren't defaults. Preserve the exact owned decision/path checks in
  `common.check_decisions` and the unresolved-runnable rejection in `manifest.py`.
- Keep `results.py` input consistency distinct from `assessment.py` and `report.py` judgments.
  A schema-valid, complete, passing report still has `qualification="unqualified"`.
- `safe_errors` may expose only fixed codes, schema-known field names, and numeric indices.
  Don't echo unknown keys, input values, paths, validator context, or Pydantic messages.
- Successful normalized output retains supplied data. Keep credentials and private endpoints
  out of inputs; references stay inert, with no process, socket, DNS, or daemon contact.
- `fixtures/` contains fictional evidence and an unapproved support matrix, not run permission.
  Don't turn a fixture, approval-reference string, or configured CI row into capacity evidence.

## Checks And Test Navigation

Use the README's **Focused Checks**, including its explicit scoped ty include: root `ty check`
doesn't include `scripts/`. Don't replace Python validation with schema-only acceptance.
Tests live under `tests/pywhales/`, relative to the repository root:

| Test | Contract |
| --- | --- |
| `test_fleet_contract.py` | Strict parsing, decisions, unsafe-input rejection before external contact |
| `test_fleet_invariants.py` | Cross-field and cross-group contradictions |
| `test_fleet_report.py` | Complete/incomplete evidence and all-outcome assessment |
| `test_fleet_cli.py` | Actual CLI with Docker absent from `PATH`, closed errors, unqualified reports |

Keep external-contact denial and success-only evidence regressions.
