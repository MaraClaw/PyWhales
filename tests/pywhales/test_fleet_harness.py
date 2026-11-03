"""Offline execution, declaration gates, exact ownership and stop behavior."""

import json
import socket
import subprocess
from pathlib import Path
from typing import Final, Literal, assert_never, override
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from scripts.fleet_harness.fake import FakeDriver, Launch, Operation
from scripts.fleet_harness.models import Plan, RunnableManifest, Step
from scripts.fleet_harness.records import (
    Cleanup,
    Observation,
    Peaks,
    RunReport,
    Schedule,
)
from scripts.fleet_harness.runner import run

FIXTURES: Final = Path(__file__).resolve().parents[2] / "scripts/fleet_harness/fixtures"


def manifest() -> RunnableManifest:
    return RunnableManifest.model_validate_json(
        (FIXTURES / "manifest.json").read_text()
    )


def plan() -> Plan:
    return Plan.model_validate_json((FIXTURES / "plan.json").read_text())


def duration_manifest() -> RunnableManifest:
    """Return a validated duration variant with the fixture's 20-second bound."""
    raw = json.loads(manifest().model_dump_json())
    raw["workload"].update(termination="duration", termination_operations=None)
    return RunnableManifest.model_validate_json(json.dumps(raw))


def duration_plan(local_seconds: float = 10.0, repetitions: int = 3) -> Plan:
    """Return validated slow inspections whose operation deadline exceeds duration."""
    raw = json.loads(plan().model_dump_json())
    raw["steps"] = [raw["steps"][1]]
    raw["steps"][0].update(local_seconds=local_seconds, readiness_seconds=None)
    raw.update(operation_timeout_seconds=30.0, repetitions=repetitions)
    return Plan.model_validate_json(json.dumps(raw))


def overlap_manifest(
    limit: int = 2,
    termination: str = "duration",
    operations: int | None = None,
    error_fraction: float = 0.5,
    assets: int = 2,
) -> RunnableManifest:
    """Return a validated manifest that admits `limit` submissions at once."""
    raw = json.loads(manifest().model_dump_json())
    raw["workload"].update(
        inflight_operations=max(2, limit),
        concurrent_submissions=limit,
        termination=termination,
        termination_operations=operations,
        operation_mix=[
            {"action": action, "fraction": 0.25, "concurrency": limit}
            for action in ("create", "inspect", "start", "remove")
        ],
    )
    raw["authorization"].update(
        allowed_actions=["create", "inspect", "start", "remove", "cleanup"],
        maximum_created_assets=assets,
    )
    raw["ownership"]["maximum_created_assets"] = assets
    raw["budgets"]["early_stop_error_fraction"] = error_fraction
    return RunnableManifest.model_validate_json(json.dumps(raw))


def at(
    arrival: float, local: float = 1.0, action: str = "inspect", **step: object
) -> dict[str, object]:
    """Return one scripted step that arrives at `arrival` on the fake clock."""
    asset = (
        {"asset": "example-container"}
        if action in {"create", "remove", "start"}
        else {}
    )
    return {
        "scenario": "load",
        "endpoint": "example-local",
        "action": action,
        "arrival_seconds": arrival,
        "local_seconds": local,
        "readiness_seconds": None,
        **asset,
        **step,
    }


def overlap_plan(*steps: dict[str, object], timeout: float = 10.0) -> Plan:
    """Return a validated one-repetition plan whose steps all carry offsets."""
    raw = json.loads(plan().model_dump_json())
    raw.update(
        steps=list(steps),
        repetitions=1,
        operation_timeout_seconds=timeout,
        maximum_samples=4 * len(steps),
    )
    return Plan.model_validate_json(json.dumps(raw))


def schedule(report: RunReport) -> Schedule:
    """Return the receipts of a report that must have run overlapping."""
    assert report.schedule is not None
    return report.schedule


def receipts(report: RunReport) -> list[tuple[float, float | None, float, str]]:
    """Return (arrival, admission, terminal, outcome) of each submission in order."""
    return [
        (r.arrival_seconds, r.admitted_seconds, r.terminal_seconds, r.outcome)
        for r in schedule(report).receipts
    ]


def spied() -> tuple[type[FakeDriver], list[tuple[str, int, float]]]:
    """Return a driver type that logs each launch and settlement, and that log."""
    log: list[tuple[str, int, float]] = []

    class Spy(FakeDriver):
        @override
        def begin(self, step: Step, launch: Launch) -> Operation:
            log.append(("begin", launch.submission, self.now))
            return super().begin(step, launch)

        @override
        def finish(self, operation: Operation) -> tuple[Observation, ...]:
            log.append(("finish", operation.launch.submission, self.now))
            return super().finish(operation)

    return Spy, log


def overlapped() -> RunReport:
    """Return a completed scheduled run with overlap and one queued waiter."""
    return run(
        overlap_manifest(2, "operation_count", 3),
        overlap_plan(at(0.0, 1.0, readiness_seconds=1.0), at(0.5, 3.0), at(1.0, 1.0)),
    )


def stopped() -> RunReport:
    """Return a scheduled run stopped by a failure while one creation was active."""
    return run(
        overlap_manifest(2),
        overlap_plan(
            at(0.0, 1.0, outcome="failed"),
            at(0.0, 3.0, "create"),
            at(0.5, 1.0),
            at(2.0, 1.0),
        ),
    )


def test_authorized_simulation_when_external_contact_is_denied() -> None:
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")),
    ):
        report = run(manifest(), plan())

    assert report.status == "completed" and report.submitted == 3
    assert report.command_count == 3 and report.connections == 0
    assert (
        report.provider_calls
        == 4
        == report.command_count + len(report.cleanup.attempted)
    )
    assert report.cleanup.removed == report.ledger and not report.cleanup.residual
    assert report.ledger[0].endpoint == "example-daemon"
    assert report.ledger[0].namespace == "synthetic-offline-run"
    assert report.ledger[0].marker == "synthetic-owner-marker"
    assert len(report.samples) == 10
    assert report.simulation and report.qualification == "unqualified"
    assert report.environment.python and report.environment.library
    assert report.environment.docker_engine is None
    assert report.environment.platform.endpoints[0].connection_ref == "withheld"
    assert {metric.name for metric in report.metrics if metric.value is None} == {
        "network_bytes",
        "daemon_cpu_cores",
        "daemon_memory_bytes",
    }


@pytest.mark.parametrize("change", ["opt_in", "provider", "endpoint", "fault", "asset"])
def test_rejection_when_authority_is_missing_precedes_driver_creation(
    change: str,
) -> None:
    recipe = plan()
    match change:
        case "opt_in":
            recipe = recipe.model_copy(update={"opt_in": False})
        case "provider":
            recipe = recipe.model_copy(update={"provider": "real"})
        case "endpoint":
            recipe = recipe.model_copy(
                update={
                    "steps": (
                        recipe.steps[0].model_copy(update={"endpoint": "not-allowed"}),
                    )
                }
            )
        case "fault":
            recipe = recipe.model_copy(
                update={
                    "steps": (
                        recipe.steps[2].model_copy(
                            update={"fault": "unauthorized-fault"}
                        ),
                    )
                }
            )
        case "asset":
            recipe = recipe.model_copy(
                update={
                    "steps": (
                        recipe.steps[0].model_copy(
                            update={"asset": "example-rollback"}
                        ),
                    )
                }
            )
    with patch(
        "scripts.fleet_harness.runner.FakeDriver",
        side_effect=AssertionError("preflight called adapter"),
    ):
        report = run(manifest(), recipe)

    assert report.status == "rejected" and report.reasons
    assert report.provider_calls == report.command_count == report.submitted == 0
    assert report.cleanup.attempted == report.ledger == ()


@pytest.mark.parametrize(
    "outcome", ["failed", "timed_out", "unknown", "cancelled", "rejected"]
)
def test_failed_samples_when_load_stops_are_not_dropped(outcome: str) -> None:
    raw = json.loads((FIXTURES / "plan.json").read_text())
    raw["steps"][0]["outcome"] = outcome
    recipe = Plan.model_validate_json(json.dumps(raw))

    report = run(manifest(), recipe)

    assert report.status == "stopped" and report.submitted == 1
    assert {sample.outcome for sample in report.samples} == {outcome}
    assert report.distributions[1].values_seconds == (0.1,)
    assert report.cleanup.removed == report.ledger


def test_deadline_when_creation_response_is_lost_retains_owned_identity() -> None:
    recipe = plan()
    recipe = recipe.model_copy(
        update={"steps": (recipe.steps[0].model_copy(update={"local_seconds": 100.0}),)}
    )

    report = run(manifest(), recipe)

    assert report.submitted == 1 and report.command_count == 1
    assert {sample.outcome for sample in report.samples} == {"timed_out"}
    assert report.distributions[1].values_seconds == (4.0,)
    assert report.elapsed_seconds == pytest.approx(4.1)
    assert report.cleanup.removed == report.ledger


def test_queue_timeout_when_never_launched_has_no_local_population() -> None:
    recipe = plan()
    recipe = recipe.model_copy(
        update={"steps": (recipe.steps[0].model_copy(update={"queue_seconds": 100.0}),)}
    )

    report = run(manifest(), recipe)

    assert report.command_count == 0 and len(report.samples) == 1
    assert report.samples[0].boundary == "queue_wait"
    assert report.samples[0].outcome == "timed_out"
    assert report.ledger == ()
    assert report.provider_calls == 0 and report.cleanup.attempted == ()


@pytest.mark.parametrize("timeout", [False, True])
def test_cleanup_failure_when_stop_occurs_reports_exact_residual(timeout: bool) -> None:
    recipe = plan().model_copy(
        update={"cleanup_seconds_per_asset": 100.0}
        if timeout
        else {"cleanup_fail_assets": ("example-container",)}
    )

    report = run(manifest(), recipe)

    assert report.cleanup.residual == report.ledger
    assert report.cleanup.attempted == report.ledger
    assert report.cleanup.removed == ()
    assert report.cleanup.timed_out is timeout
    assert report.cleanup.elapsed_seconds <= manifest().budgets.cleanup_timeout_seconds
    assert "cleanup_residual" in report.reasons
    assert (
        report.provider_calls
        == 4
        == report.command_count + len(report.cleanup.attempted)
    )
    assert all(item.slot != "example-rollback" for item in report.cleanup.attempted)


@pytest.mark.parametrize("missing", [False, True])
def test_resource_stop_when_threshold_or_collection_failure_occurs(
    missing: bool,
) -> None:
    recipe = plan()
    metrics = tuple(
        metric.model_copy(update={"value": None if missing else 2.0})
        if metric.name == "cpu_cores"
        else metric
        for metric in recipe.metrics
    )

    report = run(manifest(), recipe.model_copy(update={"metrics": metrics}))

    assert report.submitted == 1
    assert (
        "resource_metrics_unavailable" if missing else "resource_limit"
    ) in report.reasons
    assert report.cleanup.removed == report.ledger


def test_asset_cap_when_repetitions_create_more_than_authorized() -> None:
    recipe = plan()
    recipe = recipe.model_copy(update={"steps": (recipe.steps[0],), "repetitions": 3})

    report = run(manifest(), recipe)

    assert report.submitted == 2 and "asset_limit" in report.reasons
    assert len({item.identity for item in report.ledger}) == 2
    assert report.cleanup.attempted == tuple(reversed(report.ledger))


@pytest.mark.parametrize("field", ["authorization", "budgets", "ownership", "topology"])
def test_manifest_rejects_missing_safety_groups(field: str) -> None:
    raw = json.loads((FIXTURES / "manifest.json").read_text())
    del raw[field]

    with pytest.raises(ValidationError):
        RunnableManifest.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 0.0, -1.0, True])
def test_plan_rejects_nonfinite_or_invalid_deadline(value: float | bool) -> None:
    raw = json.loads((FIXTURES / "plan.json").read_text())
    raw["operation_timeout_seconds"] = value

    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(raw))


def test_unavailable_readiness_when_collection_is_omitted_is_not_zero() -> None:
    recipe = plan()
    recipe = recipe.model_copy(
        update={
            "steps": (recipe.steps[0].model_copy(update={"readiness_seconds": None}),)
        }
    )

    report = run(manifest(), recipe)

    assert report.distributions[2].recorded == report.distributions[2].unavailable == 1
    assert report.distributions[2].values_seconds == ()
    assert report.distributions[2].p95_seconds is None


def test_fake_clock_bounds_when_cleanup_has_multiple_owned_resources() -> None:
    driver = FakeDriver()

    elapsed = driver.advance(100.0, 2.0)

    assert elapsed == driver.now == 2.0


@pytest.mark.parametrize("local_seconds", [10.0, 12.0])
@pytest.mark.parametrize("repetitions", [2, 3])
def test_duration_completes_when_final_submission_reaches_boundary(
    local_seconds: float, repetitions: int
) -> None:
    # Given exact/clipped final work and a failure fraction of 1/2 at the boundary.
    recipe = duration_plan(local_seconds, repetitions)

    # When the full declared duration elapses.
    report = run(duration_manifest(), recipe)

    # Then completion retains the timeout without adding a failure/duration reason.
    assert report.status == "completed" and report.reasons == ()
    assert report.load_elapsed_seconds == 20.0
    assert report.submitted == report.command_count == report.provider_calls == 2
    assert report.outcomes.succeeded == report.outcomes.timed_out == 1
    assert report.distributions[1].values_seconds == tuple(
        sorted((local_seconds, 20.0 - local_seconds))
    )
    assert {sample.outcome for sample in report.samples[3:]} == {"timed_out"}


def test_duration_stops_when_plan_exhausts_before_cleanup_reaches_boundary() -> None:
    # Given a creation that ends just before the duration and cleanup that fills it.
    recipe = plan()
    recipe = recipe.model_copy(
        update={
            "operation_timeout_seconds": 30.0,
            "steps": (
                recipe.steps[0].model_copy(
                    update={"local_seconds": 19.9, "readiness_seconds": None}
                ),
            ),
        }
    )

    # When the short plan is run.
    report = run(duration_manifest(), recipe)

    # Then cleanup time cannot make an exhausted load plan complete.
    assert report.status == "stopped" and report.reasons == ("plan_exhausted",)
    assert report.load_elapsed_seconds == pytest.approx(19.9)
    assert report.elapsed_seconds == pytest.approx(20.0)
    assert report.cleanup.removed == report.ledger


@pytest.mark.parametrize("target", [2, 3])
def test_operation_count_stops_when_deadline_precedes_or_matches_target(
    target: int,
) -> None:
    # Given a validated operation target on or beyond the second submission.
    raw = json.loads(manifest().model_dump_json())
    raw["workload"]["termination_operations"] = target
    declaration = RunnableManifest.model_validate_json(json.dumps(raw))

    # When the second submission reaches the duration limit.
    report = run(declaration, duration_plan())

    # Then the deadline wins over both failure fraction and target completion.
    assert report.status == "stopped" and report.reasons == ("duration_limit",)
    assert report.submitted == report.command_count == report.provider_calls == 2
    assert report.load_elapsed_seconds == 20.0 and report.outcomes.timed_out == 1


def test_duration_stops_when_operation_times_out_before_workload_deadline() -> None:
    # Given an operation deadline shorter than the declared workload duration.
    recipe = duration_plan().model_copy(update={"operation_timeout_seconds": 4.0})

    # When the first operation times out before the run deadline.
    report = run(duration_manifest(), recipe)

    # Then the original failure threshold still stops the workload early.
    assert report.status == "stopped" and report.reasons == ("failure_limit",)
    assert report.load_elapsed_seconds == 4.0 and report.outcomes.timed_out == 1


@pytest.mark.parametrize("value", [2.0, None])
def test_duration_keeps_resource_stop_when_submission_reaches_boundary(
    value: float | None,
) -> None:
    # Given a resource threshold violation or unavailable measurement.
    recipe = duration_plan(local_seconds=20.0)
    recipe = recipe.model_copy(
        update={
            "metrics": tuple(
                metric.model_copy(update={"value": value})
                if metric.name == "cpu_cores"
                else metric
                for metric in recipe.metrics
            )
        }
    )

    # When load reaches the declared duration with independent safety evidence.
    report = run(duration_manifest(), recipe)

    # Then duration completion does not erase that safety reason.
    reason = "resource_metrics_unavailable" if value is None else "resource_limit"
    assert report.status == "stopped" and report.reasons == (reason,)
    assert report.load_elapsed_seconds == 20.0


def test_duration_keeps_cleanup_residual_when_load_reaches_boundary() -> None:
    # Given an owned creation clipped at duration and a failing cleanup attempt.
    recipe = plan()
    recipe = recipe.model_copy(
        update={
            "operation_timeout_seconds": 30.0,
            "steps": (recipe.steps[0].model_copy(update={"local_seconds": 30.0}),),
            "cleanup_fail_assets": ("example-container",),
        }
    )

    # When the load completes its duration but cleanup fails.
    report = run(duration_manifest(), recipe)

    # Then only cleanup stops the report, and its failed attempt counts once.
    assert report.status == "stopped" and report.reasons == ("cleanup_residual",)
    assert (
        report.provider_calls
        == 2
        == report.command_count + len(report.cleanup.attempted)
    )
    assert report.cleanup.residual == report.ledger


@pytest.mark.parametrize("status", ["completed", "stopped", "rejected"])
@pytest.mark.parametrize("provider_calls", [3, 5])
def test_report_rejects_when_provider_calls_disagree_with_attempts(
    status: str, provider_calls: int
) -> None:
    # Given internally consistent populations and status but under/overcounted calls.
    raw = json.loads(run(manifest(), plan()).model_dump_json())
    raw.update(
        status=status,
        reasons=[] if status == "completed" else ["test_stop"],
        provider_calls=provider_calls,
    )

    # When a report crosses the validation boundary.
    with pytest.raises(ValidationError) as caught:
        RunReport.model_validate_json(json.dumps(raw))

    # Then the fixed accounting error identifies the contradiction.
    assert {error["type"] for error in caught.value.errors()} == {
        "provider_call_accounting"
    }


@pytest.mark.parametrize("status", ["completed", "stopped", "rejected"])
def test_report_rejects_when_status_disagrees_with_reasons(status: str) -> None:
    # Given exact call accounting but a contradictory status/reason pair.
    raw = json.loads(run(manifest(), plan()).model_dump_json())
    raw.update(
        provider_calls=4,
        status=status,
        reasons=["test_stop"] if status == "completed" else [],
    )

    # When a report crosses the validation boundary.
    with pytest.raises(ValidationError) as caught:
        RunReport.model_validate_json(json.dumps(raw))

    # Then the fixed status error identifies the contradiction.
    assert {error["type"] for error in caught.value.errors()} == {"status_reasons"}


@pytest.mark.parametrize(
    ("defect", "code"),
    [
        ("partial", "arrival_incomplete"),
        ("decreasing", "arrival_order"),
        ("repetitions", "arrival_repetitions"),
        ("queue", "scheduled_queue"),
        ("negative", "greater_than_equal"),
        ("infinite", "finite_number"),
    ],
)
def test_plan_rejects_when_arrival_offsets_are_inconsistent(
    defect: Literal[
        "partial", "decreasing", "repetitions", "queue", "negative", "infinite"
    ],
    code: str,
) -> None:
    # Given a valid scheduled plan with one inconsistent offset declaration.
    raw = json.loads(overlap_plan(at(0.0), at(1.0)).model_dump_json())
    match defect:
        case "partial":
            raw["steps"][1]["arrival_seconds"] = None
        case "decreasing":
            raw["steps"][0]["arrival_seconds"] = 2.0
        case "repetitions":
            raw.update(repetitions=2, maximum_samples=16)
        case "queue":
            raw["steps"][0]["queue_seconds"] = 1.0
        case "negative":
            raw["steps"][0]["arrival_seconds"] = -1.0
        case "infinite":
            raw["steps"][0]["arrival_seconds"] = float("inf")
        case unreachable:
            assert_never(unreachable)

    # When the plan crosses the validation boundary.
    with pytest.raises(ValidationError) as caught:
        Plan.model_validate_json(json.dumps(raw))

    # Then a fixed error names the contradiction and nothing else.
    assert {error["type"] for error in caught.value.errors()} == {code}


def test_scheduled_simulation_when_external_contact_is_denied() -> None:
    # Given overlapping work that creates, inspects and queues behind a limit of two.
    recipe = overlap_plan(at(0.0, 1.0, "create"), at(0.0, 2.0), at(0.5, 1.0))

    # When it runs with every process and socket entry point refused.
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")),
    ):
        report = run(overlap_manifest(2, "operation_count", 3), recipe)

    # Then it stays a fake, unqualified simulation with exact call accounting.
    assert report.status == "completed" and report.submitted == 3
    assert report.connections == 0 and report.simulation
    assert report.qualification == "unqualified"
    assert report.provider_calls == report.command_count + len(report.cleanup.attempted)
    assert report.environment.platform.endpoints[0].connection_ref == "withheld"
    assert report.cleanup.removed == report.ledger and not report.cleanup.residual


def test_simultaneous_arrivals_when_limit_is_one_wait_in_fifo_order() -> None:
    # Given three submissions arriving together at a global limit of one.
    recipe = overlap_plan(at(0.0), at(0.0), at(0.0))

    # When the scheduled plan runs.
    report = run(overlap_manifest(1, "operation_count", 3), recipe)

    # Then each waits for its predecessor in plan order and the peaks show the queue.
    assert report.status == "completed" and report.reasons == ()
    assert receipts(report) == [
        (0.0, 0.0, 1.0, "succeeded"),
        (0.0, 1.0, 2.0, "succeeded"),
        (0.0, 2.0, 3.0, "succeeded"),
    ]
    assert [s.seconds for s in report.samples if s.boundary == "queue_wait"] == [
        0.0,
        1.0,
        2.0,
    ]
    assert schedule(report).peaks == Peaks(
        active=1, queued=2, per_action=(("inspect", 1),)
    )
    assert report.provider_calls == report.command_count == 3


def test_action_cap_serializes_same_action_with_global_capacity_available() -> None:
    declaration = overlap_manifest(2, "operation_count", 3)
    operations = tuple(
        op.model_copy(update={"concurrency": 1}) if op.action == "inspect" else op
        for op in declaration.workload.operation_mix
    )
    declaration = declaration.model_copy(
        update={
            "workload": declaration.workload.model_copy(
                update={"operation_mix": operations}
            )
        }
    )

    report = run(declaration, overlap_plan(at(0.0), at(0.0), at(0.0)))

    assert receipts(report) == [
        (0.0, 0.0, 1.0, "succeeded"),
        (0.0, 1.0, 2.0, "succeeded"),
        (0.0, 2.0, 3.0, "succeeded"),
    ]
    assert schedule(report).peaks == Peaks(
        active=1, queued=2, per_action=(("inspect", 1),)
    )


def test_action_cap_blocks_later_other_action_until_head_can_launch() -> None:
    declaration = overlap_manifest(2, "operation_count", 3)
    operations = tuple(
        op.model_copy(update={"concurrency": 1}) if op.action == "inspect" else op
        for op in declaration.workload.operation_mix
    )
    declaration = declaration.model_copy(
        update={
            "workload": declaration.workload.model_copy(
                update={"operation_mix": operations}
            )
        }
    )

    report = run(
        declaration, overlap_plan(at(0.0, 2.0), at(0.0), at(0.0, 1.0, "create"))
    )

    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, 2.0, 3.0, "succeeded"),
        (0.0, 2.0, 3.0, "succeeded"),
    ]
    assert schedule(report).peaks == Peaks(
        active=2, queued=2, per_action=(("create", 1), ("inspect", 1))
    )


def test_action_cap_queue_expiry_excludes_unlaunched_action_from_peaks() -> None:
    declaration = overlap_manifest(2, "operation_count", 4, error_fraction=1.0)
    operations = tuple(
        op.model_copy(update={"concurrency": 1}) if op.action == "inspect" else op
        for op in declaration.workload.operation_mix
    )
    declaration = declaration.model_copy(
        update={
            "workload": declaration.workload.model_copy(
                update={"operation_mix": operations}
            )
        }
    )

    report = run(
        declaration,
        overlap_plan(
            at(0.0, 1.0), at(0.0, 2.0), at(0.0), at(0.0, 1.0, "create"), timeout=2.0
        ),
    )

    assert receipts(report) == [
        (0.0, 0.0, 1.0, "succeeded"),
        (0.0, 1.0, 2.0, "timed_out"),
        (0.0, None, 2.0, "timed_out"),
        (0.0, None, 2.0, "timed_out"),
    ]
    assert [receipt.action for receipt in schedule(report).receipts] == [
        "inspect",
        "inspect",
        "inspect",
        "create",
    ]
    assert schedule(report).peaks == Peaks(
        active=1, queued=3, per_action=(("inspect", 1),)
    )
    assert report.command_count == 2 and not report.ledger
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_overlap_when_limit_allows_starts_next_operation_before_first_settles() -> None:
    # Given a limit of two, a two-phase first operation and a third that must wait.
    driver, log = spied()
    recipe = overlap_plan(
        at(0.0, 1.0, readiness_seconds=1.0), at(0.5, 3.0), at(1.0, 1.0)
    )

    # When the plan runs through a driver that logs launches and settlements.
    with patch("scripts.fleet_harness.runner.FakeDriver", driver):
        report = run(overlap_manifest(2), recipe)

    # Then events follow the fake clock: the second launches before the first settles.
    assert log == [
        ("begin", 1, 0.0),
        ("begin", 2, 0.5),
        ("finish", 1, 2.0),
        ("begin", 3, 2.0),
        ("finish", 3, 3.0),
        ("finish", 2, 3.5),
    ]
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.5, 0.5, 3.5, "succeeded"),
        (1.0, 2.0, 3.0, "succeeded"),
    ]
    assert schedule(report).peaks == Peaks(
        active=2, queued=1, per_action=(("inspect", 2),)
    )
    assert [
        (s.boundary, s.started_at_seconds, s.ended_at_seconds)
        for s in report.samples
        if s.submission == 1
    ] == [("queue_wait", 0.0, 0.0), ("selected_cli", 0.0, 1.0), ("readiness", 0.0, 2.0)]
    assert [s.seconds for s in report.samples if s.boundary == "queue_wait"] == [
        0.0,
        0.0,
        1.0,
    ]
    assert report.provider_calls == report.command_count == 3


@pytest.mark.parametrize(
    ("fraction", "outcome", "reasons"),
    [(0.5, "cancelled", ("failure_limit",)), (1.0, "timed_out", ())],
)
def test_queue_expiry_when_blocker_settles_at_the_same_instant(
    fraction: float, outcome: str, reasons: tuple[str, ...]
) -> None:
    # Given a queued creation whose arrival-based deadline equals its blocker's,
    # and a blocker that started late enough to run out of its own deadline.
    recipe = overlap_plan(
        at(0.0, 2.0), at(0.0, 1.5), at(0.0, 1.0, "create"), timeout=3.0
    )

    # When the blocker times out at the very instant the queued submission expires.
    report = run(overlap_manifest(1, "operation_count", 3, fraction), recipe)

    # Then the slot is not handed over: the waiter ends unlaunched, owning nothing.
    assert report.reasons == reasons
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, 2.0, 3.0, "timed_out"),
        (0.0, None, 3.0, outcome),
    ]
    assert [s.boundary for s in report.samples if s.submission == 3] == ["queue_wait"]
    assert [s.seconds for s in report.samples if s.submission == 2] == [2.0, 1.0, None]
    assert report.command_count == report.provider_calls == 2
    assert report.ledger == () and report.cleanup.attempted == ()
    assert report.outcomes.timed_out == (outcome == "timed_out") + 1
    assert report.outcomes.cancelled == (outcome == "cancelled")


def test_lifecycle_when_remove_and_start_overlap_follows_chronological_state() -> None:
    # Given a slow removal of the one created asset overlapped by two later starts.
    recipe = overlap_plan(
        at(0.0, 1.0, "create"),
        at(2.0, 2.0, "remove"),
        at(3.0, 0.5, "start"),
        at(4.0, 0.5, "start"),
    )

    # When the plan runs with room for every submission at once.
    report = run(overlap_manifest(3, "operation_count", 4), recipe)

    # Then the first start sees the asset while the removal is in flight,
    # and the second runs after the removal committed at its own arrival.
    assert receipts(report) == [
        (0.0, 0.0, 1.0, "succeeded"),
        (2.0, 2.0, 4.0, "succeeded"),
        (3.0, 3.0, 3.5, "succeeded"),
        (4.0, 4.0, 4.5, "failed"),
    ]
    assert report.status == "completed" and report.reasons == ()
    assert schedule(report).peaks == Peaks(
        active=2, queued=0, per_action=(("create", 1), ("remove", 1), ("start", 1))
    )
    assert [item.identity for item in report.ledger] == ["fake-1"]
    assert report.cleanup.attempted == () and not report.cleanup.residual
    assert report.provider_calls == report.command_count == 4


def test_early_stop_when_terminal_failure_cancels_queue_and_drains_active() -> None:
    # Given a fast failure and a slow creation, then a waiter and a later arrival.
    # When the failure stops the load while the creation is still running.
    report = stopped()

    # Then the waiter is cancelled, the creation drains to its end, and the
    # arrival after the stop never happens.
    assert report.status == "stopped" and report.reasons == ("failure_limit",)
    assert report.submitted == 3
    assert receipts(report) == [
        (0.0, 0.0, 1.0, "failed"),
        (0.0, 0.0, 3.0, "succeeded"),
        (0.5, None, 1.0, "cancelled"),
    ]
    assert schedule(report).peaks == Peaks(
        active=2, queued=1, per_action=(("create", 1), ("inspect", 1))
    )
    assert report.load_elapsed_seconds == 3.0
    assert report.elapsed_seconds == pytest.approx(3.1)
    assert [item.identity for item in report.ledger] == ["fake-2"]
    assert report.cleanup.removed == report.ledger
    assert report.command_count == 2 and report.provider_calls == 3
    assert report.outcomes.cancelled == 1 and report.outcomes.failed == 1


def test_deadline_when_arrivals_reach_load_end_excludes_them() -> None:
    # Given arrivals before, just before, exactly at, and after the 20-second end.
    recipe = overlap_plan(at(0.0), at(19.5, 2.0), at(20.0), at(25.0))

    # When the plan runs.
    report = run(overlap_manifest(2), recipe)

    # Then the last two are never submitted and the second is cut at the end.
    assert report.status == "completed" and report.reasons == ()
    assert report.submitted == 2
    assert receipts(report) == [
        (0.0, 0.0, 1.0, "succeeded"),
        (19.5, 19.5, 20.0, "timed_out"),
    ]
    assert report.load_elapsed_seconds == 20.0


def test_operation_count_when_target_is_reached_excludes_later_steps() -> None:
    # Given three scripted arrivals and a termination target of two operations.
    recipe = overlap_plan(at(0.0), at(1.0), at(2.0))

    # When the plan runs.
    report = run(overlap_manifest(2, "operation_count", 2), recipe)

    # Then the third step never arrives.
    assert report.status == "completed" and report.submitted == 2
    assert [item.arrival_seconds for item in schedule(report).receipts] == [0.0, 1.0]
    assert report.load_elapsed_seconds == 2.0


def test_zero_duration_when_operations_share_an_instant_settles_peaks() -> None:
    # Given instantaneous operations arriving together at a limit of one.
    recipe = overlap_plan(at(0.0, 0.0), at(0.0, 0.0), at(0.0, 0.0))

    # When the plan runs.
    report = run(overlap_manifest(1, "operation_count", 3), recipe)

    # Then nothing stays queued or active once the instant settles.
    assert receipts(report) == [(0.0, 0.0, 0.0, "succeeded")] * 3
    assert schedule(report).peaks == Peaks(
        active=0, queued=0, per_action=(("inspect", 0),)
    )
    assert report.status == "completed"


def test_asset_cap_when_admission_would_exceed_it_cancels_the_waiter() -> None:
    # Given three creations arriving together against a cap of two assets.
    recipe = overlap_plan(
        at(0.0, 2.0, "create"), at(0.0, 2.0, "create"), at(0.0, 2.0, "create")
    )

    # When the third reaches admission.
    report = run(overlap_manifest(3, assets=2), recipe)

    # Then the stop cancels it without an asset and the two active ones drain.
    assert report.status == "stopped" and report.reasons == ("asset_limit",)
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, None, 0.0, "cancelled"),
    ]
    assert [item.identity for item in report.ledger] == ["fake-1", "fake-2"]
    assert report.cleanup.attempted == tuple(reversed(report.ledger))
    assert report.command_count == 2 and report.provider_calls == 4


@pytest.mark.parametrize("change", ["opt_in", "provider", "endpoint", "asset"])
def test_scheduled_plan_when_authority_is_missing_is_rejected_before_driver(
    change: str,
) -> None:
    # Given a scheduled plan under a manifest whose limit permits overlap.
    recipe = overlap_plan(at(0.0, 1.0, "create"), at(0.0))
    match change:
        case "opt_in":
            recipe = recipe.model_copy(update={"opt_in": False})
        case "provider":
            recipe = recipe.model_copy(update={"provider": "real"})
        case "endpoint":
            recipe = recipe.model_copy(
                update={
                    "steps": (
                        recipe.steps[0].model_copy(update={"endpoint": "not-allowed"}),
                        recipe.steps[1],
                    )
                }
            )
        case "asset":
            recipe = recipe.model_copy(
                update={
                    "steps": (
                        recipe.steps[0].model_copy(
                            update={"asset": "example-rollback"}
                        ),
                        recipe.steps[1],
                    )
                }
            )

    # When it is run with driver and scheduler construction forbidden.
    with (
        patch(
            "scripts.fleet_harness.runner.FakeDriver",
            side_effect=AssertionError("preflight called adapter"),
        ),
        patch(
            "scripts.fleet_harness.runner.Simulation",
            side_effect=AssertionError("preflight scheduled"),
        ),
    ):
        report = run(overlap_manifest(2), recipe)

    # Then declared concurrency grants nothing and an empty receipt set remains.
    assert report.status == "rejected" and report.reasons
    assert report.provider_calls == report.command_count == report.submitted == 0
    assert report.cleanup.attempted == report.ledger == ()
    assert report.schedule == Schedule(peaks=Peaks(active=0, queued=0), receipts=())


def test_failure_when_scheduled_operation_raises_still_runs_exact_cleanup() -> None:
    # Given two creations in flight and a driver whose first settlement raises.
    cleaned: list[Cleanup] = []

    class Failing(FakeDriver):
        @override
        def finish(self, operation: Operation) -> tuple[Observation, ...]:
            raise RuntimeError(f"injected {operation.launch.submission}")

        @override
        def cleanup(self, plan: Plan, seconds: float) -> Cleanup:
            cleaned.append(result := super().cleanup(plan, seconds))
            return result

    recipe = overlap_plan(at(0.0, 1.0, "create"), at(0.5, 3.0, "create"))

    # When the exception escapes the load loop.
    with (
        patch("scripts.fleet_harness.runner.FakeDriver", Failing),
        pytest.raises(RuntimeError, match="injected 1"),
    ):
        run(overlap_manifest(2), recipe)

    # Then every acquired identity is still removed once, newest first.
    assert len(cleaned) == 1
    assert [item.identity for item in cleaned[0].attempted] == ["fake-2", "fake-1"]
    assert cleaned[0].removed == cleaned[0].attempted and not cleaned[0].residual


@pytest.mark.parametrize("timeout", [False, True])
def test_cleanup_failure_when_scheduled_run_ends_reports_exact_residual(
    timeout: bool,
) -> None:
    # Given an otherwise clean overlapping run whose cleanup fails or runs out of time.
    recipe = overlap_plan(at(0.0, 1.0, "create"), at(0.0)).model_copy(
        update={"cleanup_seconds_per_asset": 100.0}
        if timeout
        else {"cleanup_fail_assets": ("example-container",)}
    )

    # When it runs.
    report = run(overlap_manifest(2, "operation_count", 2), recipe)

    # Then residuals, call accounting and receipts all stay visible.
    assert report.status == "stopped" and report.reasons == ("cleanup_residual",)
    assert report.cleanup.residual == report.ledger == report.cleanup.attempted
    assert report.cleanup.timed_out is timeout
    assert report.provider_calls == report.command_count + len(report.cleanup.attempted)
    assert {item[3] for item in receipts(report)} == {"succeeded"}
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_report_when_scheduled_round_trips_and_old_sequential_json_imports() -> None:
    # Given a scheduled report and a sequential one in the pre-schedule JSON shape.
    scheduled = overlapped()
    old = json.loads(run(manifest(), plan()).model_dump_json())
    del old["schedule"]
    for step in old["steps"]:
        del step["arrival_seconds"]

    # When both cross the validation boundary.
    imported = RunReport.model_validate_json(json.dumps(old))

    # Then the schedule survives exactly and the old report is simply sequential.
    assert RunReport.model_validate_json(scheduled.model_dump_json()) == scheduled
    assert imported.schedule is None
    assert all(step.arrival_seconds is None for step in imported.steps)


@pytest.mark.parametrize(
    ("defect", "code"),
    [
        ("swap", "schedule_order"),
        ("fifo", "schedule_order"),
        ("peaks", "schedule_peaks"),
        ("action_peaks", "schedule_peaks"),
        ("action_limit", "schedule_concurrency"),
        ("action_mismatch", "schedule_binding"),
        ("action_undeclared", "schedule_binding"),
        ("limit", "schedule_concurrency"),
        ("sample_time", "schedule_times"),
        ("sample_order", "schedule_times"),
        ("deadline", "schedule_times"),
        ("window", "schedule_times"),
        ("outcome", "schedule_times"),
        ("offset", "schedule_binding"),
        ("repetitions", "schedule_binding"),
        ("removed", "schedule_binding"),
        ("cancelled", "schedule_binding"),
        ("sequential", "schedule_binding"),
    ],
)
def test_report_rejects_when_scheduled_import_is_contradictory(
    defect: Literal[
        "swap",
        "fifo",
        "peaks",
        "action_peaks",
        "action_limit",
        "action_mismatch",
        "action_undeclared",
        "limit",
        "sample_time",
        "sample_order",
        "deadline",
        "window",
        "outcome",
        "offset",
        "repetitions",
        "removed",
        "cancelled",
        "sequential",
    ],
    code: str,
) -> None:
    # Given the JSON form of a sound overlapping report and one contradiction.
    raw = json.loads(overlapped().model_dump_json())
    rows = raw["schedule"]["receipts"]
    match defect:
        case "swap":
            rows[0], rows[1] = rows[1], rows[0]
        case "fifo":
            rows[1]["admitted_seconds"] = 2.5
        case "peaks":
            raw["schedule"]["peaks"]["active"] = 1
        case "action_peaks":
            raw["schedule"]["peaks"]["per_action"][0][1] = 1
        case "action_limit":
            next(
                op
                for op in raw["workload"]["operation_mix"]
                if op["action"] == "inspect"
            )["concurrency"] = 1
        case "action_mismatch" | "action_undeclared":
            action = "create" if defect == "action_mismatch" else "pull"
            rows[0]["action"] = action
            raw["schedule"]["peaks"]["per_action"] = (
                [[action, 1], ["inspect", 2]]
                if action == "create"
                else [["inspect", 2], [action, 1]]
            )
        case "limit":
            raw["workload"]["concurrent_submissions"] = 1
            for operation in raw["workload"]["operation_mix"]:
                operation["concurrency"] = 1
        case "sample_time":
            sample = next(
                s
                for s in raw["samples"]
                if s["submission"] == 3 and s["boundary"] == "selected_cli"
            )
            sample["started_at_seconds"] = 2.5
        case "sample_order":
            first = next(
                i
                for i, s in enumerate(raw["samples"])
                if s["submission"] == 1 and s["boundary"] == "selected_cli"
            )
            raw["samples"][first : first + 2] = raw["samples"][first : first + 2][::-1]
        case "deadline":
            raw["operation_timeout_seconds"] = 1.0
        case "window":
            raw["workload"]["duration_seconds"] = 1.0
        case "outcome":
            rows[0]["outcome"] = "failed"
        case "offset":
            raw["steps"][1]["arrival_seconds"] = 0.6
        case "repetitions":
            raw["repetitions"] = 2
        case "removed":
            raw["schedule"] = None
        case "cancelled":
            raw = json.loads(stopped().model_dump_json())
            raw.update(status="completed", reasons=[])
        case "sequential":
            raw = json.loads(run(manifest(), plan()).model_dump_json())
            raw["schedule"] = {"peaks": {"active": 0, "queued": 0}, "receipts": []}
        case unreachable:
            assert_never(unreachable)

    # When the report crosses the validation boundary.
    with pytest.raises(ValidationError) as caught:
        RunReport.model_validate_json(json.dumps(raw))

    # Then one fixed code names the first contradiction.
    assert {error["type"] for error in caught.value.errors()} == {code}


def test_queue_lease_retry_rejoins_after_simultaneous_arrival() -> None:
    recipe = overlap_plan(
        at(0.0, 2.5),
        at(0.0, 0.5, queue_lease_seconds=1.0, max_queue_attempts=4),
        at(1.0, 0.5),
    )
    report = run(overlap_manifest(1, "operation_count", 3), recipe)

    assert receipts(report) == [
        (0.0, 0.0, 2.5, "succeeded"),
        (0.0, 3.0, 3.5, "succeeded"),
        (1.0, 2.5, 3.0, "succeeded"),
    ]
    attempt_rows = schedule(report).receipts[1].attempts
    assert attempt_rows is not None
    assert [(a.enqueued_seconds, a.ended_seconds) for a in attempt_rows] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
        (3.0, 3.0),
    ]
    assert [s.seconds for s in report.samples if s.boundary == "queue_wait"] == [
        0.0,
        3.0,
        1.5,
    ]
    assert report.submitted == report.command_count == report.outcomes.succeeded == 3
    assert RunReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize(
    ("attempts", "timeout", "end"), [(2, 10.0, 2.0), (10, 2.5, 2.5)]
)
def test_queue_lease_exhaustion_or_original_deadline_ends_unlaunched(
    attempts: int, timeout: float, end: float
) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3, error_fraction=1.0),
        overlap_plan(
            at(0.0, 0.1),
            at(0.1, 9.0),
            at(0.1, queue_lease_seconds=1.0, max_queue_attempts=attempts),
            timeout=timeout,
        ),
    )
    row = schedule(report).receipts[2]
    assert row.admitted_seconds is None
    assert row.terminal_seconds == end + 0.1
    assert row.outcome == "timed_out"
    assert report.command_count == 2 and report.submitted == 3


def test_launched_lease_submission_never_retries() -> None:
    report = run(
        overlap_manifest(1, "operation_count", 1),
        overlap_plan(at(0.0, 3.0, queue_lease_seconds=0.5, max_queue_attempts=5)),
    )
    row = schedule(report).receipts[0]
    assert row.attempts is not None
    assert [(a.enqueued_seconds, a.ended_seconds) for a in row.attempts] == [(0.0, 0.0)]
    assert row.outcome == "succeeded" and report.command_count == 1


@pytest.mark.parametrize("backoff", [None, 1.0])
def test_early_stop_cancels_retry_pending_at_same_instant(
    backoff: float | None,
) -> None:
    report = run(
        overlap_manifest(2, "operation_count", 4),
        overlap_plan(
            at(0.0, 5.0),
            at(0.0, 5.0),
            at(
                0.0,
                queue_lease_seconds=1.0,
                max_queue_attempts=3,
                queue_retry_backoff_seconds=backoff,
            ),
            at(0.0, queue_lease_seconds=1.0, max_queue_attempts=1),
        ),
    )
    assert [r.outcome for r in schedule(report).receipts] == [
        "succeeded",
        "succeeded",
        "cancelled",
        "timed_out",
    ]
    assert schedule(report).receipts[2].terminal_seconds == 1.0
    if backoff is not None:
        assert schedule(report).receipts[2].model_dump()["backoffs"] == (
            {"started_seconds": 1.0, "ended_seconds": 1.0},
        )


@pytest.mark.parametrize("defect", ["missing", "gap", "overlap", "lease", "count"])
def test_retry_report_rejects_tampered_lineage(defect: str) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 2),
        overlap_plan(
            at(0.0, 1.5), at(0.0, queue_lease_seconds=1.0, max_queue_attempts=2)
        ),
    )
    raw = json.loads(report.model_dump_json())
    row = raw["schedule"]["receipts"][1]
    if defect == "missing":
        del row["attempts"]
    elif defect == "gap":
        row["attempts"][1]["enqueued_seconds"] = 1.1
    elif defect == "overlap":
        row["attempts"][1]["enqueued_seconds"] = 0.9
    elif defect == "lease":
        raw["steps"][1]["queue_lease_seconds"] = 0.75
    else:
        raw["steps"][1]["max_queue_attempts"] = 1
    with pytest.raises(ValidationError):
        RunReport.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "fields",
    [
        {"queue_lease_seconds": 0},
        {"queue_lease_seconds": -1},
        {"queue_lease_seconds": float("inf")},
        {"queue_lease_seconds": True},
        {"max_queue_attempts": 0},
        {"max_queue_attempts": True},
        {"max_queue_attempts": 2},
    ],
)
def test_step_rejects_invalid_lease_declarations(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        overlap_plan(at(0.0) | fields)


@pytest.mark.parametrize("field", ["queue_lease_seconds", "max_queue_attempts"])
def test_unscheduled_step_rejects_lease_fields(field: str) -> None:
    raw = json.loads(plan().model_dump_json())
    raw["steps"][0][field] = 1
    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(raw))


def test_legacy_digests_and_report_shapes_remain_unchanged() -> None:
    sequential = run(manifest(), plan())
    scheduled = overlapped()
    assert (
        sequential.operation_digest
        == "sha256:f3e241c7fc5af586e20ffd9295ddbdbb2b0ce25c3e4550336643ec6b05fe3e6f"
    )
    assert (
        scheduled.operation_digest
        == "sha256:db9fac335b961217aa419cb33f2af1a1dc3de2471af9fae4ab731d16dfb7425f"
    )
    assert "queue_lease_seconds" not in sequential.steps[0].model_dump()
    assert "max_queue_attempts" not in sequential.steps[0].model_dump()
    assert "attempts" not in schedule(scheduled).receipts[0].model_dump()
    assert "backoffs" not in schedule(scheduled).receipts[0].model_dump()
    assert "version" not in schedule(scheduled).model_dump()
    assert "queue_retry_backoff_seconds" not in sequential.steps[0].model_dump()


def test_lease_declaration_changes_operation_digest() -> None:
    declaration = overlap_manifest(1, "operation_count", 1)
    baseline = run(declaration, overlap_plan(at(0.0)))
    leased = run(
        declaration,
        overlap_plan(at(0.0, queue_lease_seconds=1.0, max_queue_attempts=2)),
    )
    assert baseline.operation_digest != leased.operation_digest
