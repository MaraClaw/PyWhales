"""Conditional no-go and semantic comparison behavior with no candidate launches."""

import json
import socket
import subprocess
from typing import Literal, assert_never
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from scripts.fleet_harness.experiment import (
    Baseline,
    Comparison,
    Experiment,
    Semantics,
    decide,
)
from scripts.fleet_harness.runner import run
from tests.pywhales.test_fleet_harness import (
    at,
    duration_manifest,
    duration_plan,
    manifest,
    overlap_manifest,
    overlap_plan,
    overlapped,
    plan,
)


def semantics() -> Semantics:
    return Semantics(
        authentication="declared",
        endpoint_verification="declared",
        api_versions=("v1.0",),
        errors="typed",
        permissions="equivalent",
        result_shape="inventory",
        stream_ordering="ordered",
        backpressure="bounded",
        byte_accounting="bytes",
        ownership="owned",
        early_close="local_only",
        cancellation="local_only",
        timeout="bounded_local",
        ambiguous_mutation="unknown_no_retry_no_rollback",
        confidentiality="no_credentials_argv_or_payloads",
    )


def test_no_go_when_baseline_is_absent_contacts_nothing() -> None:
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
    ):
        decision = decide(Experiment())

    assert decision.decision == "no_go"
    assert "real_material_overhead_baseline_missing" in decision.reasons
    assert decision.candidate_launches == decision.connections == 0
    assert decision.selected_route == "cli"


def test_matched_simulation_when_contracts_agree_does_not_authorize_adoption() -> None:
    result = run(manifest(), plan())
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    decision = decide(Experiment(comparison=comparison))

    assert (
        decision.comparison_status == "matched_simulation" and not decision.mismatches
    )
    assert decision.decision == "no_go" and decision.simulation
    assert decision.evidence.comparison == comparison
    assert decision.candidate_launches == 0


@pytest.mark.parametrize(
    "field",
    [
        "cancellation",
        "stream_ordering",
        "backpressure",
        "byte_accounting",
        "early_close",
        "timeout",
        "permissions",
        "authentication",
        "endpoint_verification",
        "result_shape",
        "ownership",
        "errors",
        "api_versions",
        "ambiguous_mutation",
        "confidentiality",
    ],
)
def test_semantic_mismatch_when_candidate_is_faster_rejects(field: str) -> None:
    result = run(manifest(), plan())
    contract = semantics()
    candidate = contract.model_copy(
        update={field: ("v2.0",) if field == "api_versions" else "incompatible"}
    )
    comparison = Comparison(
        cli=result,
        candidate=result.model_copy(update={"elapsed_seconds": 0.01}),
        cli_contract=contract,
        candidate_contract=candidate,
    )

    decision = decide(Experiment(comparison=comparison))

    assert decision.comparison_status == "rejected"
    assert f"contract.{field}" in decision.mismatches
    assert (
        decision.selected_route == "cli"
        and decision.compose_buildx_route == "cli_unchanged"
    )


def test_scope_mismatch_when_operation_inputs_change_rejects() -> None:
    result = run(manifest(), plan())
    candidate = result.model_copy(update={"operation_digest": "sha256:" + "f" * 64})
    comparison = Comparison(
        cli=result,
        candidate=candidate,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    decision = decide(Experiment(comparison=comparison))

    assert decision.comparison_status == "rejected"
    assert "scope.operation_digest" in decision.mismatches


def test_outcome_mismatch_when_candidate_fails_below_stop_threshold_rejects() -> None:
    recipe = plan()
    candidate_recipe = recipe.model_copy(
        update={
            "steps": (
                *recipe.steps[:2],
                recipe.steps[2].model_copy(update={"outcome": "failed"}),
            )
        }
    )
    cli = run(manifest(), recipe)
    candidate = run(manifest(), candidate_recipe)
    comparison = Comparison(
        cli=cli,
        candidate=candidate,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    decision = decide(Experiment(comparison=comparison))

    assert candidate.status == "completed"
    assert (
        decision.comparison_status == "rejected"
        and "observed_outcomes" in decision.mismatches
    )


@pytest.mark.parametrize(
    "provenance,seconds,reason",
    [
        ("simulation", (2.0,), "real_material_overhead_baseline_unqualified"),
        ("real", (), "material_overhead_entry_unmet"),
        ("real", (0.1,), "material_overhead_entry_unmet"),
        ("real", (2.0,), "real_binding_and_deadline_adapter_unqualified"),
    ],
)
def test_entry_gate_when_baseline_is_declared_never_launches(
    provenance: str, seconds: tuple[float, ...], reason: str
) -> None:
    baseline = Baseline.model_validate(
        {
            "provenance": provenance,
            "representative": True,
            "independent_review_ref": "declaration-only",
            "operator_objective_ref": "declaration-only",
            "workload_digest": "sha256:" + "b" * 64,
            "environment_digest": "sha256:" + "c" * 64,
            "transport_seconds": seconds,
            "material_overhead_seconds": 1.0,
        }
    )

    decision = decide(Experiment(baseline=baseline))

    assert reason in decision.reasons
    assert decision.decision == "no_go" and decision.candidate_launches == 0


def test_matched_simulation_when_duration_finishes_is_not_incomplete() -> None:
    # Given equal full-duration runs with a final boundary timeout observation.
    result = run(duration_manifest(), duration_plan(12.0, 2))
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    # When prerecorded simulations are compared.
    decision = decide(Experiment(comparison=comparison))

    # Then completion is usable for comparison, never authorization to adopt.
    assert decision.comparison_status == "matched_simulation"
    assert decision.mismatches == () and decision.decision == "no_go"
    assert decision.candidate_launches == 0


@pytest.mark.parametrize("route", ["cli", "candidate"])
@pytest.mark.parametrize(
    "defect", ["provider_calls", "completed", "stopped", "rejected"]
)
def test_experiment_rejects_when_imported_report_is_incoherent(
    route: str, defect: Literal["provider_calls", "completed", "stopped", "rejected"]
) -> None:
    # Given valid populations with incoherent accounting or status in either route.
    result = run(manifest(), plan())
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    raw = json.loads(Experiment(comparison=comparison).model_dump_json())
    for report in (raw["comparison"]["cli"], raw["comparison"]["candidate"]):
        report["provider_calls"] = 4
    report = raw["comparison"][route]
    match defect:
        case "provider_calls":
            report["provider_calls"] = 5
            code = "provider_call_accounting"
        case "completed" | "stopped" | "rejected":
            report.update(
                status=defect, reasons=["test_stop"] if defect == "completed" else []
            )
            code = "status_reasons"
        case unreachable:
            assert_never(unreachable)

    # When the serialized experiment is imported.
    with pytest.raises(ValidationError) as caught:
        Experiment.model_validate_json(json.dumps(raw))

    # Then nested RunReport validation rejects the specific route with fixed codes.
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        (("comparison", route), code)
    ]


def test_matched_overlap_when_contracts_agree_stays_no_go_without_launches() -> None:
    # Given equal overlapping simulations with receipts, peaks and a queued waiter.
    result = overlapped()
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    # When the prerecorded comparison is decided with every contact path refused.
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
    ):
        decision = decide(Experiment(comparison=comparison))

    # Then overlap evidence never promotes the route beyond a simulated no-go.
    assert decision.comparison_status == "matched_simulation"
    assert decision.mismatches == () and decision.decision == "no_go"
    assert decision.selected_route == "cli" and decision.simulation
    assert decision.candidate_launches == decision.connections == 0
    assert decision.evidence.comparison == comparison


@pytest.mark.parametrize("change", ["offsets", "sequential"])
def test_scope_mismatch_when_arrival_offsets_differ_rejects(change: str) -> None:
    # Given the same operations run with other offsets, or with none at all.
    steps = (at(0.0), at(0.5))
    other = (at(0.0), at(0.6)) if change == "offsets" else (at(0.0), at(0.5))
    cli = run(overlap_manifest(2), overlap_plan(*steps))
    recipe = overlap_plan(*other)
    if change == "sequential":
        recipe = recipe.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(update={"arrival_seconds": None})
                    for step in recipe.steps
                )
            }
        )
    candidate = run(overlap_manifest(2), recipe)
    comparison = Comparison(
        cli=cli,
        candidate=candidate,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )

    # When the comparison is decided.
    decision = decide(Experiment(comparison=comparison))

    # Then the operation digest binds the offsets and the candidate is rejected.
    assert cli.operation_digest != candidate.operation_digest
    assert decision.comparison_status == "rejected"
    assert "scope.operation_digest" in decision.mismatches
    assert decision.decision == "no_go" and decision.candidate_launches == 0


def test_experiment_rejects_when_imported_scheduled_report_is_tampered() -> None:
    # Given a matched overlapping comparison whose candidate misreports its peaks.
    result = overlapped()
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    raw = json.loads(Experiment(comparison=comparison).model_dump_json())
    raw["comparison"]["candidate"]["schedule"]["peaks"]["active"] = 1

    # When the serialized experiment is imported.
    with pytest.raises(ValidationError) as caught:
        Experiment.model_validate_json(json.dumps(raw))

    # Then nested validation rejects only the candidate with the fixed peaks code.
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        (("comparison", "candidate", "schedule"), "schedule_peaks")
    ]
