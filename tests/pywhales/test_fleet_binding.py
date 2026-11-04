"""Report content binding across offline import, export and comparison."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from scripts.fleet_harness.experiment import Baseline, Comparison, Experiment, decide
from scripts.fleet_harness.export import ExportRefused, export_result
from scripts.fleet_harness.models import RunnableManifest
from scripts.fleet_harness.records import RunReport
from scripts.fleet_harness.runner import run
from tests.pywhales.test_fleet_harness import (
    at,
    manifest,
    overlap_manifest,
    overlap_plan,
    plan,
)
from tests.pywhales.test_transport_experiment import semantics


def test_binding_uses_canonical_json_for_completed_and_rejected_runs() -> None:
    raw = json.loads(manifest().model_dump_json())
    raw["identity"]["owners"]["maintainer"] = "maintainer-é"
    declaration = RunnableManifest.model_validate_json(json.dumps(raw))
    for recipe in (plan(), plan().model_copy(update={"opt_in": False})):
        report = run(declaration, recipe)
        assert report.binding is not None
        for model, digest in (
            (declaration, report.binding.manifest_digest),
            (report.environment, report.binding.environment_digest),
        ):
            payload = json.loads(model.model_dump_json())
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            assert digest == "sha256:" + hashlib.sha256(encoded).hexdigest()
        assert RunReport.model_validate_json(report.model_dump_json()) == report
        assert report.environment.platform.endpoints[0].connection_ref == "withheld"


@pytest.mark.parametrize(
    ("group", "field", "value"),
    [
        ("authorization", "record_id", "other-record"),
        ("budgets", "readiness_definition", "other-definition"),
        ("ownership", "teardown_owner", "other-owner"),
        ("topology", "dns_expectation", "other-dns"),
        ("identity", "profile", "other-profile"),
        ("platform", "required_features", ["other-feature"]),
        ("root", "support_row", "other-row"),
        ("connection", "connection_ref", "other-reference"),
        ("artifact", "destination", "other-destination"),
    ],
)
def test_export_refuses_changed_declaration_under_same_revision(
    group: str, field: str, value: str | list[str]
) -> None:
    declaration = manifest()
    report = run(declaration, plan())
    raw = json.loads(declaration.model_dump_json())
    target = {
        "root": raw,
        "connection": raw["platform"]["endpoints"][0],
        "artifact": raw["identity"]["artifacts"][0],
    }.get(group, raw.get(group))
    target[field] = value
    changed = RunnableManifest.model_validate_json(json.dumps(raw))

    with pytest.raises(ExportRefused):
        export_result(report, changed)


def test_export_accepts_normalized_equivalent_and_preserves_array_order() -> None:
    declaration = manifest()
    raw = json.loads(declaration.model_dump_json())
    reordered_keys = json.dumps(raw, sort_keys=True, indent=4)
    equivalent = RunnableManifest.model_validate_json(reordered_keys)
    report = run(declaration, plan())
    assert export_result(report, equivalent).provenance == "synthetic"

    raw["workload"]["endpoint_aliases"].reverse()
    changed = RunnableManifest.model_validate_json(json.dumps(raw))
    with pytest.raises(ExportRefused):
        export_result(report, changed)


@pytest.mark.parametrize(
    "defect", ["environment", "digest", "partial", "version", "bool", "null"]
)
def test_report_import_rejects_invalid_binding(defect: str) -> None:
    raw = json.loads(run(manifest(), plan()).model_dump_json())
    if defect == "environment":
        raw["environment"]["python"] = "altered"
    elif defect == "digest":
        raw["binding"]["environment_digest"] = "sha256:" + "f" * 64
    elif defect == "partial":
        del raw["binding"]["manifest_digest"]
    elif defect == "version":
        raw["binding"]["version"] = 2
    elif defect == "bool":
        raw["binding"]["version"] = True
    else:
        raw["binding"] = None
    with pytest.raises(ValidationError):
        RunReport.model_validate_json(json.dumps(raw))


def test_legacy_unbound_report_imports_but_cannot_export_or_match() -> None:
    declaration = manifest()
    raw = json.loads(run(declaration, plan()).model_dump_json())
    del raw["binding"]
    legacy = RunReport.model_validate_json(json.dumps(raw))
    assert legacy.binding is None
    assert RunReport.model_validate_json(legacy.model_dump_json()).binding is None
    with pytest.raises(ExportRefused):
        export_result(legacy, declaration)
    comparison = Comparison(
        cli=legacy,
        candidate=legacy,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    decision = decide(Experiment(comparison=comparison))
    assert decision.comparison_status == "rejected"
    assert "scope.binding_missing" in decision.mismatches
    assert (
        decision.decision == "no_go"
        and decision.candidate_launches == decision.connections == 0
    )


def test_comparison_rejects_same_revision_when_manifest_policy_changes() -> None:
    declaration = manifest()
    raw = json.loads(declaration.model_dump_json())
    raw["authorization"]["record_id"] = "other-record"
    changed = RunnableManifest.model_validate_json(json.dumps(raw))
    comparison = Comparison(
        cli=run(declaration, plan()),
        candidate=run(changed, plan()),
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    decision = decide(Experiment(comparison=comparison))
    assert decision.comparison_status == "rejected"
    assert "scope.manifest_digest" in decision.mismatches


def test_comparison_rejects_independently_bound_environment_change() -> None:
    report = run(manifest(), plan())
    raw = json.loads(report.model_dump_json())
    raw["environment"]["python"] = "another-interpreter"
    encoded = json.dumps(
        raw["environment"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    raw["binding"]["environment_digest"] = (
        "sha256:" + hashlib.sha256(encoded).hexdigest()
    )
    candidate = RunReport.model_validate_json(json.dumps(raw))
    comparison = Comparison(
        cli=report,
        candidate=candidate,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    decision = decide(Experiment(comparison=comparison))
    assert decision.comparison_status == "rejected"
    assert "scope.environment_digest" in decision.mismatches


@pytest.mark.parametrize("mismatch", ["none", "workload", "environment"])
def test_baseline_must_match_cli_workload_and_environment(mismatch: str) -> None:
    report = run(manifest(), plan())
    assert report.binding is not None
    baseline = Baseline(
        provenance="real",
        representative=True,
        independent_review_ref="review",
        operator_objective_ref="objective",
        workload_digest="sha256:" + "f" * 64
        if mismatch == "workload"
        else report.workload_digest,
        environment_digest="sha256:" + "f" * 64
        if mismatch == "environment"
        else report.binding.environment_digest,
        transport_seconds=(2.0,),
        material_overhead_seconds=1.0,
    )
    comparison = Comparison(
        cli=report,
        candidate=report,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    decision = decide(Experiment(baseline=baseline, comparison=comparison))
    if mismatch == "none":
        assert (
            decision.comparison_status == "matched_simulation"
            and not decision.mismatches
        )
    else:
        assert decision.comparison_status == "rejected"
        assert f"baseline.{mismatch}_digest" in decision.mismatches
    assert decision.decision == "no_go" and decision.selected_route == "cli"


EXPONENTIAL = {
    "queue_lease_seconds": 1.0,
    "max_queue_attempts": 3,
    "queue_retry_exponential": {"initial_seconds": 0.5, "max_seconds": 1.0},
}


def test_exponential_policy_keeps_binding_version_and_declaration_digests() -> None:
    declaration = overlap_manifest(1, "operation_count", 2)
    reports = [
        run(declaration, overlap_plan(at(0.0, 1.5), at(0.0) | policy))
        for policy in ({}, EXPONENTIAL)
    ]
    rejected = run(
        declaration,
        overlap_plan(at(0.0, 1.5), at(0.0) | EXPONENTIAL).model_copy(
            update={"opt_in": False}
        ),
    )
    for report in (*reports, rejected):
        assert report.binding is not None and report.binding.version == 1
        assert report.binding == reports[0].binding
        assert RunReport.model_validate_json(report.model_dump_json()) == report
    assert len({report.operation_digest for report in (*reports, rejected)}) == 2
    assert reports[-1].schedule is not None
    assert reports[-1].schedule.model_dump()["version"] == 3
    assert export_result(reports[-1], declaration).provenance == "synthetic"


def test_exponential_report_cannot_export_under_changed_declaration() -> None:
    declaration = overlap_manifest(1, "operation_count", 2)
    report = run(declaration, overlap_plan(at(0.0, 1.5), at(0.0) | EXPONENTIAL))
    raw = json.loads(declaration.model_dump_json())
    raw["authorization"]["record_id"] = "other-record"
    with pytest.raises(ExportRefused):
        export_result(report, RunnableManifest.model_validate_json(json.dumps(raw)))


def test_comparison_rejects_exponential_candidate_with_changed_policy() -> None:
    declaration = overlap_manifest(1, "operation_count", 2)
    reports = [
        run(
            declaration,
            overlap_plan(
                at(0.0, 1.5),
                at(0.0)
                | EXPONENTIAL
                | {
                    "queue_retry_exponential": {
                        "initial_seconds": 0.5,
                        "max_seconds": cap,
                    }
                },
            ),
        )
        for cap in (1.0, 2.0)
    ]
    decision = decide(
        Experiment(
            comparison=Comparison(
                cli=reports[0],
                candidate=reports[1],
                cli_contract=semantics(),
                candidate_contract=semantics(),
            )
        )
    )
    assert "scope.operation_digest" in decision.mismatches
    assert decision.decision == "no_go" and decision.candidate_launches == 0
