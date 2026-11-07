"""Evidence completeness is independent of qualification or claimed provenance."""

import json
from pathlib import Path
from typing import Final

import pytest
from pydantic import JsonValue, TypeAdapter

from scripts.fleet_contract.manifest import Manifest
from scripts.fleet_contract.report import Evidence, classify
from scripts.fleet_contract.results import RunResult
from scripts.fleet_contract.support import SupportMatrix
from scripts.fleet_harness.export import export_result
from scripts.fleet_harness.models import RunnableManifest
from scripts.fleet_harness.records import RunReport
from scripts.fleet_harness.runner import run
from tests.pywhales.test_fleet_harness import at, overlap_manifest, overlap_plan

FIXTURES: Final = (
    Path(__file__).resolve().parents[2] / "scripts/fleet_contract/fixtures"
)
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


def evidence(result: RunResult) -> Evidence:
    """Bind fixture observations to the actual typed manifest and support matrix."""
    return Evidence(
        manifest=Manifest.model_validate_json(
            (FIXTURES / "synthetic-runnable.json").read_text()
        ),
        support=SupportMatrix.model_validate_json(
            (FIXTURES / "support-matrix.json").read_text()
        ),
        result=result,
    )


def test_complete_example_when_synthetic_never_qualifies() -> None:
    result = RunResult.model_validate_json(
        (FIXTURES / "synthetic-result.json").read_text()
    )

    report = classify(evidence(result))

    assert report.schema_valid and report.evidence_complete
    assert report.qualification == "unqualified"
    assert report.criteria == "passed"
    assert {
        "synthetic_non_authorizing",
        "support_unapproved",
        "independent_review_required",
    } <= set(report.flags)
    assert report.distributions[0].measured_count == 2
    assert report.distributions[0].percentiles[-1].seconds == 0.2


def test_incomplete_fixture_when_failures_are_retained() -> None:
    result = RunResult.model_validate_json(
        (FIXTURES / "incomplete-result.json").read_text()
    )

    report = classify(evidence(result))

    assert not report.evidence_complete
    assert report.qualification == "unqualified" and report.criteria == "unknown"
    assert report.evidence.result.outcomes.accounted == 7
    assert (
        result.outcomes.failed,
        result.outcomes.rejected,
        result.outcomes.cancelled,
        result.outcomes.unknown,
        result.outcomes.timed_out,
    ) == (1, 1, 1, 1, 1)
    assert {
        "latency_missing",
        "latency_excluded",
        "cleanup_incomplete",
        "declared_missing_evidence",
        "declared_excluded_evidence",
        "high_water_missing",
    } <= set(report.flags)
    assert report.distributions[0].expected_count == 7
    assert report.distributions[0].measured_count == 5


@pytest.mark.parametrize(
    ("field", "value", "flag"),
    [
        ("latency", [], "latency_missing"),
        ("high_water", [], "high_water_missing"),
        ("elapsed_seconds", None, "duration_stability_missing"),
        ("authorization_record", None, "authorization_record_mismatch"),
        ("observed_versions", None, "environment_missing_or_mismatched"),
        ("cleanup_receipt_ref", None, "receipts_missing"),
        ("excluded_evidence", ["failed-samples"], "declared_excluded_evidence"),
        (
            "outcomes",
            {
                "succeeded": 1,
                "failed": 0,
                "rejected": 0,
                "cancelled": 0,
                "unknown": 0,
                "timed_out": 0,
            },
            "outcome_accounting_incomplete",
        ),
        ("endpoint_aliases", ["unknown"], "endpoint_alias_mismatch"),
        ("early_stopped", True, "early_stopped"),
    ],
)
def test_unqualified_when_required_evidence_is_missing(
    field: str, value: JsonValue, flag: str
) -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    data[field] = value
    result = RunResult.model_validate_json(json.dumps(data))

    report = classify(evidence(result))

    assert not report.evidence_complete
    assert report.qualification == "unqualified"
    assert flag in report.flags


def test_omitted_failure_when_denominator_is_preserved() -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    data["outcomes"] = {
        "succeeded": 1,
        "failed": 1,
        "rejected": 0,
        "cancelled": 0,
        "unknown": 0,
        "timed_out": 0,
    }
    result = RunResult.model_validate_json(json.dumps(data))

    report = classify(evidence(result))

    assert report.qualification == "unqualified"
    assert not report.evidence_complete
    assert "outcome_samples_mismatch" in report.flags


def test_failed_criteria_when_complete_evidence_exceeds_latency_budget() -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    latencies = data["latency"]
    assert isinstance(latencies, list) and isinstance(latencies[0], dict)
    samples = latencies[0]["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0]["seconds"] = 9.0

    report = classify(evidence(RunResult.model_validate_json(json.dumps(data))))

    assert report.evidence_complete
    assert report.criteria == "failed" and report.qualification == "unqualified"


def test_operator_claim_when_all_evidence_is_self_declared() -> None:
    original = evidence(
        RunResult.model_validate_json((FIXTURES / "synthetic-result.json").read_text())
    )
    data = JSON_OBJECT.validate_json(original.model_dump_json())
    manifest, result = data["manifest"], data["result"]
    assert isinstance(manifest, dict) and isinstance(result, dict)
    manifest["provenance"] = result["provenance"] = "operator_declared"
    auth = manifest["authorization"]
    assert isinstance(auth, dict)
    auth["purpose"] = "operator_declared"

    report = classify(Evidence.model_validate_json(json.dumps(data)))

    assert report.evidence_complete
    assert report.qualification == "unqualified"
    assert report.review_required == "independent_maintainer_and_operator"


def test_supported_claim_when_review_references_are_not_authentication() -> None:
    original = evidence(
        RunResult.model_validate_json((FIXTURES / "synthetic-result.json").read_text())
    )
    data = JSON_OBJECT.validate_json(original.model_dump_json())
    matrix = data["support"]
    assert isinstance(matrix, dict)
    rows = matrix["rows"]
    assert isinstance(rows, list) and isinstance(rows[2], dict)
    rows[2].update(
        {
            "status": "supported",
            "provenance": "operator_declared",
            "approval_ref": "claimed-review",
            "evidence_refs": ["claimed-receipt"],
        }
    )

    report = classify(Evidence.model_validate_json(json.dumps(data)))

    assert report.evidence_complete
    assert report.qualification == "unqualified"
    assert "independent_review_required" in report.flags


def settle(data: dict[str, JsonValue], boundaries: set[str], submission: int) -> None:
    """Fail one submission at the named latency boundaries of a fixture result."""
    latencies = data["latency"]
    assert isinstance(latencies, list)
    for item in latencies:
        assert isinstance(item, dict) and isinstance(item["samples"], list)
        for sample in item["samples"]:
            assert isinstance(sample, dict)
            if item["boundary"] in boundaries and sample["submission"] == submission:
                sample.update({"outcome": "failed", "seconds": 9.0})


def test_consistent_failure_timings_when_the_full_distribution_is_retained() -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    data["outcomes"] = {
        "succeeded": 1,
        "failed": 1,
        "rejected": 0,
        "cancelled": 0,
        "unknown": 0,
        "timed_out": 0,
    }
    settle(data, {"queue_wait", "selected_cli", "readiness"}, 1)

    report = classify(evidence(RunResult.model_validate_json(json.dumps(data))))

    assert report.evidence_complete
    assert report.criteria == "failed"
    assert report.distributions[0].percentiles[-1].seconds == 9.0


def test_failure_timings_when_the_full_distribution_is_retained() -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    data["outcomes"] = {
        "succeeded": 1,
        "failed": 1,
        "rejected": 0,
        "cancelled": 0,
        "unknown": 0,
        "timed_out": 0,
    }
    settle(data, {"queue_wait"}, 1)

    report = classify(evidence(RunResult.model_validate_json(json.dumps(data))))

    assert not report.evidence_complete and report.criteria == "unknown"
    assert report.qualification == "unqualified"
    assert "submission_outcome_conflict" in report.flags
    assert report.distributions[0].percentiles[-1].seconds == 9.0


def test_submission_outcome_when_other_boundaries_disagree_with_aggregate() -> None:
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    settle(data, {"selected_cli"}, 2)

    report = classify(evidence(RunResult.model_validate_json(json.dumps(data))))

    assert not report.evidence_complete
    assert {"submission_outcome_conflict", "outcome_samples_mismatch"} <= set(
        report.flags
    )


def exported(report: RunReport, declaration: RunnableManifest) -> Evidence:
    """Bind a harness export to the contract manifest and the unapproved matrix."""
    return Evidence(
        manifest=Manifest.model_validate_json(declaration.model_dump_json()),
        support=SupportMatrix.model_validate_json(
            (FIXTURES / "support-matrix.json").read_text()
        ),
        result=export_result(report, declaration),
    )


def test_overlapping_export_when_synthetic_never_qualifies() -> None:
    # Given a completed overlapping simulation with a queued waiter.
    declaration = overlap_manifest(2, "operation_count", 3)
    simulation = run(
        declaration,
        overlap_plan(at(0.0, 1.0, readiness_seconds=0.5), at(0.0, 2.0), at(0.5, 1.0)),
    )

    # When its synthetic export is classified against the unapproved matrix.
    report = classify(exported(simulation, declaration))

    # Then overlap never turns simulation into permission or qualification.
    assert simulation.status == "completed" and simulation.schedule is not None
    assert report.qualification == "unqualified" and not report.evidence_complete
    assert report.evidence.result.provenance == "synthetic"
    assert report.evidence.result.authorization_record is None
    assert {
        "synthetic_non_authorizing",
        "support_unapproved",
        "receipts_missing",
        "authorization_record_mismatch",
    } <= set(report.flags)


@pytest.mark.parametrize(
    ("fraction", "outcome"), [(0.5, "cancelled"), (1.0, "timed_out")]
)
def test_unlaunched_submission_when_exported_is_counted_once(
    fraction: float, outcome: str
) -> None:
    # Given a queued creation that never launches: cancelled by the stop, or expired.
    declaration = overlap_manifest(1, "operation_count", 3, fraction)
    simulation = run(
        declaration,
        overlap_plan(
            at(0.0, 1.0, readiness_seconds=0.5),
            at(0.0, 1.0, readiness_seconds=1.0),
            at(0.0, 1.0, "create"),
            timeout=3.0,
        ),
    )

    # When the simulation is exported and classified.
    report = classify(exported(simulation, declaration))

    # Then the third is one all-submissions outcome with no launch evidence.
    result = report.evidence.result
    assert result.populations.all_submissions == result.outcomes.accounted == 3
    assert result.populations.launched_cli == 2
    assert result.outcomes.cancelled == (outcome == "cancelled")
    assert result.outcomes.timed_out == 1 + (outcome == "timed_out")
    assert report.qualification == "unqualified"
    assert not {
        "outcome_samples_mismatch",
        "submission_outcome_conflict",
        "latency_excluded",
        "latency_missing",
    } & set(report.flags)


@pytest.mark.parametrize("backoff", [None, 1.0, "exponential", "jitter"])
def test_retry_export_counts_logical_submissions_and_queue_samples_once(
    backoff: float | str | None,
) -> None:
    declaration = overlap_manifest(1, "operation_count", 2)
    simulation = run(
        declaration,
        overlap_plan(
            at(0.0, 1.5),
            at(
                0.0,
                queue_lease_seconds=1.0,
                max_queue_attempts=2,
                queue_retry_backoff_seconds=backoff
                if isinstance(backoff, float)
                else None,
                queue_retry_exponential={
                    "initial_seconds": 1.0,
                    "max_seconds": 2.0,
                    **(
                        {"jitter": {"seed": 7, "fraction": 0.25}}
                        if backoff == "jitter"
                        else {}
                    ),
                }
                if backoff in ("exponential", "jitter")
                else None,
            ),
        ),
    )
    result = export_result(simulation, declaration)
    queue = next(item for item in result.latency if item.boundary == "queue_wait")
    assert result.populations.all_submissions == result.outcomes.accounted == 2
    expected = 1.5 if backoff is None else 2.0
    if backoff == "jitter":
        # Independently recorded SHA-256 unit for (seed=7, submission=2, attempt=1).
        expected = 1.0 + 1.0 * (1 - 0.25 * 0.30873262866772744)
    assert [(sample.submission, sample.seconds) for sample in queue.samples] == [
        (1, 0.0),
        (2, expected),
    ]
    assert result.populations.launched_cli == 2


@pytest.mark.parametrize(
    ("fraction", "outcome"), [(0.5, "cancelled"), (1.0, "timed_out")]
)
def test_backoff_terminal_export_counts_no_extra_launch(
    fraction: float, outcome: str
) -> None:
    declaration = overlap_manifest(1, "operation_count", 3, fraction)
    simulation = run(
        declaration,
        overlap_plan(
            at(0.0, 0.1),
            at(0.1, 9.0),
            at(
                0.1,
                queue_lease_seconds=1.0,
                max_queue_attempts=4,
                queue_retry_backoff_seconds=8.0,
            ),
            timeout=3.0,
        ),
    )
    report = classify(exported(simulation, declaration))
    result = report.evidence.result
    assert result.populations.all_submissions == result.outcomes.accounted == 3
    assert result.populations.launched_cli == 2
    assert simulation.schedule is not None
    assert simulation.schedule.receipts[2].outcome == outcome
    assert report.qualification == "unqualified"
    assert not {
        "outcome_samples_mismatch",
        "submission_outcome_conflict",
        "latency_excluded",
        "latency_missing",
    } & set(report.flags)
