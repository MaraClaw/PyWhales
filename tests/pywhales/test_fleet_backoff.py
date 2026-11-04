"""Deterministic delayed retry histories and independent import contracts."""

import hashlib
import json
import socket
import subprocess
from collections.abc import Iterable
from typing import Literal, assert_never
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from scripts.fleet_harness.experiment import Comparison, Experiment, decide
from scripts.fleet_harness.models import Plan, QueueRetryExponential, Step
from scripts.fleet_harness.records import Receipt, RunReport, Schedule
from scripts.fleet_harness.runner import run
from scripts.fleet_harness.timeline import lineage
from tests.pywhales.test_fleet_harness import (
    at,
    manifest,
    overlap_manifest,
    overlap_plan,
    plan,
    receipts,
    schedule,
)
from tests.pywhales.test_transport_experiment import semantics

POLICY = {
    "queue_lease_seconds": 1.0,
    "max_queue_attempts": 4,
    "queue_retry_backoff_seconds": 1.0,
}


def test_backoff_ties_preserve_phase_arrival_and_submission_order() -> None:
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")),
    ):
        report = run(
            overlap_manifest(1, "operation_count", 4),
            overlap_plan(
                at(0.0, 2.0),
                at(0.0, 0.25) | POLICY,
                at(0.0, 0.25) | POLICY,
                at(2.0, 0.25),
            ),
        )
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, 2.25, 2.5, "succeeded"),
        (0.0, 2.5, 2.75, "succeeded"),
        (2.0, 2.0, 2.25, "succeeded"),
    ]
    rows = schedule(report).receipts
    assert rows[1].model_dump()["backoffs"] == (
        {"started_seconds": 1.0, "ended_seconds": 2.0},
    )
    assert rows[1].model_dump()["attempts"] == (
        {"enqueued_seconds": 0.0, "ended_seconds": 1.0},
        {"enqueued_seconds": 2.0, "ended_seconds": 2.25},
    )
    assert schedule(report).peaks.model_dump() == {
        "active": 1,
        "queued": 2,
        "per_action": (("inspect", 1),),
    }
    assert [s.seconds for s in report.samples if s.boundary == "queue_wait"] == [
        0.0,
        2.25,
        2.5,
        0.0,
    ]
    assert report.submitted == report.command_count == report.outcomes.succeeded == 4
    assert report.connections == 0 and report.qualification == "unqualified"
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_backoff_is_outside_capacity_and_does_not_block_later_arrivals() -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3),
        overlap_plan(
            at(0.0, 1.5),
            at(0.0) | POLICY | {"queue_retry_backoff_seconds": 2.0},
            at(1.25, 0.5),
        ),
    )
    assert receipts(report) == [
        (0.0, 0.0, 1.5, "succeeded"),
        (0.0, 3.0, 4.0, "succeeded"),
        (1.25, 1.5, 2.0, "succeeded"),
    ]
    assert schedule(report).peaks.queued == schedule(report).peaks.active == 1


@pytest.mark.parametrize(
    ("delay", "attempts", "timeout", "end", "spans", "backoffs"),
    [
        (0.5, 2, 10.0, 2.5, [(0.1, 1.1), (1.6, 2.6)], [(1.1, 1.6)]),
        (2.0, 5, 3.0, 3.0, [(0.1, 1.1)], [(1.1, 3.1)]),
        (8.0, 5, 3.0, 3.0, [(0.1, 1.1)], [(1.1, 3.1)]),
        (0.5, 5, 2.0, 2.0, [(0.1, 1.1), (1.6, 2.1)], [(1.1, 1.6)]),
    ],
)
def test_backoff_deadlines_and_final_attempt_exhaustion(
    delay: float,
    attempts: int,
    timeout: float,
    end: float,
    spans: list[tuple[float, float]],
    backoffs: list[tuple[float, float]],
) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3, error_fraction=1.0),
        overlap_plan(
            at(0.0, 0.1),
            at(0.1, 9.0),
            at(0.1)
            | POLICY
            | {"max_queue_attempts": attempts, "queue_retry_backoff_seconds": delay},
            timeout=timeout,
        ),
    )
    row = schedule(report).receipts[2]
    assert row.admitted_seconds is None and row.outcome == "timed_out"
    assert row.terminal_seconds == end + 0.1
    assert row.attempts is not None and row.backoffs is not None
    assert [(s.enqueued_seconds, s.ended_seconds) for s in row.attempts] == spans
    assert [(s.started_seconds, s.ended_seconds) for s in row.backoffs] == backoffs
    assert report.submitted == 3 and report.command_count == 2


def test_backoff_load_deadline_does_not_reset_at_retry() -> None:
    report = run(
        overlap_manifest(1),
        overlap_plan(
            at(18.0, 2.0),
            at(18.0) | POLICY | {"queue_retry_backoff_seconds": 3.0},
        ),
    )
    row = schedule(report).receipts[1]
    assert row.admitted_seconds is None and row.terminal_seconds == 20.0
    assert row.model_dump()["backoffs"] == (
        {"started_seconds": 19.0, "ended_seconds": 20.0},
    )


def test_pending_deadline_expires_before_same_time_original_arrival() -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3, error_fraction=0.5),
        overlap_plan(
            at(0.0, 2.0),
            at(0.0) | POLICY | {"queue_retry_backoff_seconds": 8.0},
            at(3.0),
            timeout=3.0,
        ),
    )
    assert report.submitted == 2 and report.command_count == 1
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "succeeded"),
        (0.0, None, 3.0, "timed_out"),
    ]
    assert report.reasons == ("failure_limit",)


@pytest.mark.parametrize(
    ("outcome", "end", "deadline", "valid"),
    [
        ("timed_out", 1.5, 1.5, True),
        ("timed_out", 2.0, 2.0, True),
        ("timed_out", 1.5, 3.0, False),
        ("timed_out", 2.0, 3.0, False),
        ("cancelled", 1.0, 3.0, True),
        ("cancelled", 1.5, 3.0, True),
        ("cancelled", 2.5, 3.0, False),
        ("cancelled", 2.0, 1.5, False),
        ("failed", 1.5, 3.0, False),
    ],
)
def test_independent_pending_terminal_intervals(
    outcome: str, end: float, deadline: float, valid: bool
) -> None:
    row = Receipt.model_validate_json(
        json.dumps(
            {
                "submission": 1,
                "action": "inspect",
                "arrival_seconds": 0.0,
                "admitted_seconds": None,
                "terminal_seconds": end,
                "outcome": outcome,
                "attempts": [{"enqueued_seconds": 0.0, "ended_seconds": 1.0}],
                "backoffs": [{"started_seconds": 1.0, "ended_seconds": end}],
            }
        )
    )
    step = Step.model_validate_json(json.dumps(at(0.0) | POLICY))
    assert lineage(row, step, deadline) is valid


@pytest.mark.parametrize("stop", [1.0, 2.0, 4.0])
def test_stop_cancels_pending_backoff_immediately_then_drains_owned_work(
    stop: float,
) -> None:
    report = run(
        overlap_manifest(2, "operation_count", 5),
        overlap_plan(
            at(0.0, stop, outcome="failed"),
            at(0.0, 5.0, "create"),
            at(0.0) | POLICY | {"queue_retry_backoff_seconds": 3.0},
            at(0.0) | POLICY,
            at(6.0),
        ),
    )
    assert report.status == "stopped" and report.reasons == ("failure_limit",)
    assert report.submitted == 4 and report.command_count == 2
    row = schedule(report).receipts[2]
    assert row.outcome == "cancelled" and row.terminal_seconds == stop
    assert row.backoffs is not None
    assert [(s.started_seconds, s.ended_seconds) for s in row.backoffs] == (
        [] if stop == 1.0 else [(1.0, stop)]
    )
    assert report.load_elapsed_seconds == 5.0
    assert report.cleanup.attempted == report.cleanup.removed == report.ledger
    assert len(report.ledger) == 1 and not report.cleanup.residual
    assert RunReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown"])
def test_launched_work_with_backoff_never_retries(outcome: str) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 1),
        overlap_plan(at(0.0, 3.0, outcome=outcome) | POLICY),
    )
    row = schedule(report).receipts[0]
    assert row.backoffs == () and row.outcome == outcome
    assert row.model_dump()["attempts"] == (
        {"enqueued_seconds": 0.0, "ended_seconds": 0.0},
    )
    assert report.command_count == 1


def test_import_rejects_pending_backoff_surviving_another_cancellation() -> None:
    report = run(
        overlap_manifest(2, "operation_count", 4),
        overlap_plan(
            at(0.0, 2.0, outcome="failed"),
            at(0.0, 5.0),
            at(0.0) | POLICY | {"queue_retry_backoff_seconds": 8.0},
            at(0.0) | POLICY | {"queue_retry_backoff_seconds": 8.0},
            timeout=3.0,
        ),
    )
    assert receipts(report) == [
        (0.0, 0.0, 2.0, "failed"),
        (0.0, 0.0, 3.0, "timed_out"),
        (0.0, None, 2.0, "cancelled"),
        (0.0, None, 2.0, "cancelled"),
    ]
    assert RunReport.model_validate_json(report.model_dump_json()) == report
    raw = json.loads(report.model_dump_json())
    row = raw["schedule"]["receipts"][2]
    row.update(outcome="timed_out", terminal_seconds=3.0)
    row["backoffs"][-1]["ended_seconds"] = 3.0
    sample = next(s for s in raw["samples"] if s["submission"] == 3)
    sample.update(outcome="timed_out", seconds=3.0, ended_at_seconds=3.0)
    raw["outcomes"]["cancelled"] -= 1
    raw["outcomes"]["timed_out"] += 1

    with pytest.raises(ValidationError) as caught:
        RunReport.model_validate_json(json.dumps(raw))
    assert "schedule_times" in {error["type"] for error in caught.value.errors()}


@pytest.mark.parametrize(
    "value", [0, -1, True, "1", float("nan"), float("inf"), -float("inf")]
)
def test_backoff_requires_positive_finite_number(value: object) -> None:
    with pytest.raises(ValidationError):
        overlap_plan(at(0.0) | POLICY | {"queue_retry_backoff_seconds": value})


@pytest.mark.parametrize(
    "fields",
    [
        {"queue_retry_backoff_seconds": 1.0},
        {"queue_retry_backoff_seconds": 1.0, "queue_lease_seconds": 1.0},
        {
            "queue_retry_backoff_seconds": 1.0,
            "queue_lease_seconds": 1.0,
            "max_queue_attempts": 1,
        },
    ],
)
def test_backoff_requires_scheduled_lease_and_multiple_attempts(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        overlap_plan(at(0.0) | fields)
    raw = json.loads(plan().model_dump_json())
    raw["steps"][0].update(POLICY)
    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(raw))


def test_independently_authored_history_validates_without_scheduler_replay() -> None:
    history = Schedule.model_validate_json(
        json.dumps(
            {
                "version": 2,
                "peaks": {"active": 1, "queued": 1, "per_action": [["inspect", 1]]},
                "receipts": [
                    {
                        "submission": 1,
                        "action": "inspect",
                        "arrival_seconds": 0.0,
                        "admitted_seconds": 0.0,
                        "terminal_seconds": 2.0,
                        "outcome": "succeeded",
                    },
                    {
                        "submission": 2,
                        "action": "inspect",
                        "arrival_seconds": 0.0,
                        "admitted_seconds": 2.5,
                        "terminal_seconds": 3.0,
                        "outcome": "succeeded",
                        "attempts": [
                            {"enqueued_seconds": 0.0, "ended_seconds": 1.0},
                            {"enqueued_seconds": 2.0, "ended_seconds": 2.5},
                        ],
                        "backoffs": [{"started_seconds": 1.0, "ended_seconds": 2.0}],
                    },
                    {
                        "submission": 3,
                        "action": "inspect",
                        "arrival_seconds": 2.0,
                        "admitted_seconds": 2.0,
                        "terminal_seconds": 2.5,
                        "outcome": "succeeded",
                    },
                ],
            }
        )
    )
    step = Step.model_validate_json(json.dumps(at(0.0, 0.5) | POLICY))
    with patch(
        "scripts.fleet_harness.scheduler.Simulation.run",
        side_effect=AssertionError("replay"),
    ):
        assert lineage(history.receipts[1], step, 10.0)
        assert not lineage(history.receipts[1], step, 2.0)
        assert not lineage(
            history.receipts[1],
            step.model_copy(update={"queue_retry_backoff_seconds": 0.5}),
            10.0,
        )
    malformed = json.loads(history.model_dump_json())
    malformed["receipts"][1]["admitted_seconds"] = 1.5
    with pytest.raises(ValidationError):
        Schedule.model_validate_json(json.dumps(malformed))


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "gap",
        "overlap",
        "delay",
        "deadline",
        "exhausted",
        "version",
        "downgrade",
        "upgrade",
        "peaks",
        "queue_sample",
        "terminal",
    ],
)
def test_backoff_report_rejects_tampered_imports(
    defect: Literal[
        "missing",
        "gap",
        "overlap",
        "delay",
        "deadline",
        "exhausted",
        "version",
        "downgrade",
        "upgrade",
        "peaks",
        "queue_sample",
        "terminal",
    ],
) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 2),
        overlap_plan(at(0.0, 2.5), at(0.0, 0.5) | POLICY),
    )
    raw = json.loads(report.model_dump_json())
    row = raw["schedule"]["receipts"][1]
    match defect:
        case "missing":
            del row["backoffs"]
        case "gap":
            row["backoffs"][0]["started_seconds"] = 1.1
        case "overlap":
            row["backoffs"][0]["ended_seconds"] = 2.1
        case "delay":
            raw["steps"][1]["queue_retry_backoff_seconds"] = 0.5
        case "deadline":
            raw["operation_timeout_seconds"] = 2.0
        case "exhausted":
            raw["steps"][1]["max_queue_attempts"] = 1
        case "version":
            raw["schedule"]["version"] = 3
        case "downgrade":
            del raw["schedule"]["version"]
        case "upgrade":
            del raw["steps"][1]["queue_retry_backoff_seconds"]
            del row["backoffs"]
        case "peaks":
            raw["schedule"]["peaks"]["queued"] = 2
        case "queue_sample":
            next(
                s
                for s in raw["samples"]
                if s["submission"] == 2 and s["boundary"] == "queue_wait"
            )["seconds"] = 0.5
        case "terminal":
            row["backoffs"].append({"started_seconds": 2.5, "ended_seconds": 3.0})
        case unreachable:
            assert_never(unreachable)
    with pytest.raises(ValidationError):
        RunReport.model_validate_json(json.dumps(raw))


def test_backoff_digest_binds_comparison_without_changing_binding() -> None:
    declaration = overlap_manifest(1, "operation_count", 1)
    reports = [
        run(
            declaration,
            overlap_plan(at(0.0) | POLICY | {"queue_retry_backoff_seconds": delay}),
        )
        for delay in (0.5, 1.0)
    ]
    operations = [
        {
            "scenario": "load",
            "endpoint": "example-local",
            "action": "inspect",
            "asset": None,
            "fault": None,
            "arrival_seconds": 0.0,
            **POLICY,
        }
    ]
    assert (
        reports[1].operation_digest
        == "sha256:"
        + hashlib.sha256(json.dumps(operations, sort_keys=True).encode()).hexdigest()
    )
    assert reports[0].operation_digest != reports[1].operation_digest
    assert reports[0].binding == reports[1].binding
    assert reports[1].binding is not None and reports[1].binding.version == 1
    assert schedule(reports[1]).model_dump()["version"] == 2
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
    assert (
        decision.decision == "no_go"
        and decision.connections == decision.candidate_launches == 0
    )


def exponential(initial: float, cap: float, attempts: int = 4) -> dict[str, object]:
    """Return a one-second lease that retries with capped doubling delays."""
    return {
        "queue_lease_seconds": 1.0,
        "max_queue_attempts": attempts,
        "queue_retry_exponential": {"initial_seconds": initial, "max_seconds": cap},
    }


def spans(items: Iterable[tuple[float, float]]) -> list[object]:
    """Compare fake-clock interval pairs within float noise."""
    return [pytest.approx(item) for item in items]


def test_exponential_delays_double_then_cap_and_retry_ties_after_phase_end() -> None:
    with (
        patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
        patch.object(socket, "socket", side_effect=AssertionError("socket")),
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")),
    ):
        report = run(
            overlap_manifest(1, "operation_count", 2),
            overlap_plan(at(0.0, 6.0), at(0.0, 0.5) | exponential(0.5, 1.5)),
        )
    row = schedule(report).receipts[1]
    assert receipts(report) == [
        (0.0, 0.0, 6.0, "succeeded"),
        (0.0, 6.0, 6.5, "succeeded"),
    ]
    assert row.attempts is not None and row.backoffs is not None
    assert [(a.enqueued_seconds, a.ended_seconds) for a in row.attempts] == [
        (0.0, 1.0),
        (1.5, 2.5),
        (3.5, 4.5),
        (6.0, 6.0),
    ]
    assert [(b.started_seconds, b.ended_seconds) for b in row.backoffs] == [
        (1.0, 1.5),
        (2.5, 3.5),
        (4.5, 6.0),
    ]
    assert schedule(report).model_dump()["version"] == 3
    assert schedule(report).peaks.model_dump() == {
        "active": 1,
        "queued": 1,
        "per_action": (("inspect", 1),),
    }
    assert [s.seconds for s in report.samples if s.boundary == "queue_wait"] == [
        0.0,
        6.0,
    ]
    assert report.connections == 0 and report.qualification == "unqualified"
    assert RunReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize(
    ("policy", "attempts", "timeout", "end", "queued", "waits"),
    [
        (
            (0.25, 0.5),
            4,
            10.0,
            5.35,
            [(0.1, 1.1), (1.35, 2.35), (2.85, 3.85), (4.35, 5.35)],
            [(1.1, 1.35), (2.35, 2.85), (3.85, 4.35)],
        ),
        ((2.0, 8.0), 5, 3.0, 3.1, [(0.1, 1.1)], [(1.1, 3.1)]),
        ((1.5, 8.0), 5, 3.0, 3.1, [(0.1, 1.1), (2.6, 3.1)], [(1.1, 2.6)]),
        ((8.0, 8.0), 5, 3.0, 3.1, [(0.1, 1.1)], [(1.1, 3.1)]),
        ((1e308, 1.7e308), 5, 3.0, 3.1, [(0.1, 1.1)], [(1.1, 3.1)]),
        ((1.7e308, 1.7e308), 5, 3.0, 3.1, [(0.1, 1.1)], [(1.1, 3.1)]),
    ],
)
def test_exponential_terminates_without_another_attempt_at_cap_deadline_or_exhaustion(
    policy: tuple[float, float],
    attempts: int,
    timeout: float,
    end: float,
    queued: list[tuple[float, float]],
    waits: list[tuple[float, float]],
) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3, error_fraction=1.0),
        overlap_plan(
            at(0.0, 0.1),
            at(0.1, 9.0),
            at(0.1) | exponential(*policy, attempts=attempts),
            timeout=timeout,
        ),
    )
    row = schedule(report).receipts[2]
    assert row.admitted_seconds is None and row.outcome == "timed_out"
    assert row.terminal_seconds == pytest.approx(end)
    assert row.attempts is not None and row.backoffs is not None
    assert [(a.enqueued_seconds, a.ended_seconds) for a in row.attempts] == spans(
        queued
    )
    assert [(b.started_seconds, b.ended_seconds) for b in row.backoffs] == spans(waits)
    assert report.submitted == 3 and report.command_count == 2
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_exponential_delay_never_overflows_and_follows_the_closed_form() -> None:
    huge = QueueRetryExponential(initial_seconds=1e308, max_seconds=1.7e308)
    assert [huge.delay(n) for n in (1, 2, 3, 10**12)] == [1e308] + [1.7e308] * 3
    tiny = QueueRetryExponential(initial_seconds=5e-324, max_seconds=1.7e308)
    assert tiny.delay(1) == 5e-324 and tiny.delay(2) == 1e-323
    assert tiny.delay(10**12) == 1.7e308
    subnormal = QueueRetryExponential(initial_seconds=1e-323, max_seconds=2.5e-323)
    assert [subnormal.delay(n) for n in (1, 2, 3)] == [1e-323, 2e-323, 2.5e-323]
    plain = QueueRetryExponential(initial_seconds=0.375, max_seconds=100.0)
    assert [plain.delay(n) for n in range(1, 12)] == [
        min(100.0, 0.375 * 2 ** (n - 1)) for n in range(1, 12)
    ]


@pytest.mark.parametrize("opt_in", [True, False])
@pytest.mark.parametrize(
    ("policies", "version"),
    [
        ([], None),
        ([POLICY], 2),
        ([exponential(0.5, 1.0)], 3),
        ([POLICY, exponential(0.5, 1.0)], 3),
    ],
)
def test_schedule_version_follows_the_policies_of_the_plan(
    policies: list[dict[str, object]], version: int | None, opt_in: bool
) -> None:
    recipe = overlap_plan(at(0.0, 1.0), *(at(0.0) | item for item in policies))
    report = run(
        overlap_manifest(1, "operation_count", len(recipe.steps)),
        recipe.model_copy(update={"opt_in": opt_in}),
    )
    assert schedule(report).model_dump().get("version") == version
    assert report.status == ("completed" if opt_in else "rejected")
    if not opt_in:
        assert schedule(report).receipts == ()
        assert report.provider_calls == report.command_count == report.submitted == 0
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_exponential_and_fixed_steps_mix_in_one_plan_with_independent_delays() -> None:
    report = run(
        overlap_manifest(1, "operation_count", 3, error_fraction=1.0),
        overlap_plan(
            at(0.0, 9.0),
            at(0.0) | exponential(0.5, 4.0, attempts=3),
            at(0.0) | POLICY | {"max_queue_attempts": 3},
            timeout=20.0,
        ),
    )
    exp_row, fixed_row = schedule(report).receipts[1:]
    assert exp_row.backoffs is not None and fixed_row.backoffs is not None
    assert [
        round(b.ended_seconds - b.started_seconds, 6) for b in exp_row.backoffs
    ] == [
        0.5,
        1.0,
    ]
    assert [
        round(b.ended_seconds - b.started_seconds, 6) for b in fixed_row.backoffs
    ] == [
        1.0,
        1.0,
    ]
    assert schedule(report).model_dump()["version"] == 3


@pytest.mark.parametrize("stop", [1.0, 2.0, 4.0])
def test_stop_cancels_pending_exponential_retry_then_drains_owned_work(
    stop: float,
) -> None:
    report = run(
        overlap_manifest(2, "operation_count", 5),
        overlap_plan(
            at(0.0, stop, outcome="failed"),
            at(0.0, 5.0, "create"),
            at(0.0) | exponential(3.0, 3.0, attempts=2),
            at(0.0) | POLICY,
            at(6.0),
        ),
    )
    assert report.status == "stopped" and report.reasons == ("failure_limit",)
    assert report.submitted == 4 and report.command_count == 2
    row = schedule(report).receipts[2]
    assert row.outcome == "cancelled" and row.terminal_seconds == stop
    assert row.backoffs is not None
    assert [(b.started_seconds, b.ended_seconds) for b in row.backoffs] == (
        [] if stop == 1.0 else [(1.0, stop)]
    )
    assert report.load_elapsed_seconds == 5.0
    assert report.cleanup.attempted == report.cleanup.removed == report.ledger
    assert len(report.ledger) == 1 and not report.cleanup.residual
    assert RunReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown"])
def test_launched_work_with_exponential_policy_never_retries(outcome: str) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 1),
        overlap_plan(at(0.0, 3.0, outcome=outcome) | exponential(0.5, 2.0)),
    )
    row = schedule(report).receipts[0]
    assert row.backoffs == () and row.outcome == outcome
    assert len(row.attempts or ()) == 1 and report.command_count == 1


@pytest.mark.parametrize(
    "value", [0, -1, True, "1", float("nan"), float("inf"), -float("inf")]
)
@pytest.mark.parametrize("field", ["initial_seconds", "max_seconds"])
def test_exponential_requires_positive_finite_numbers(
    field: str, value: object
) -> None:
    policy = {"initial_seconds": 1.0, "max_seconds": 4.0} | {field: value}
    with pytest.raises(ValidationError):
        overlap_plan(
            at(0.0) | exponential(1.0, 4.0) | {"queue_retry_exponential": policy}
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"initial_seconds": 2.0, "max_seconds": 1.0},
        {"initial_seconds": 1.0},
        {"max_seconds": 1.0},
        {"initial_seconds": 1.0, "max_seconds": 2.0, "factor": 2.0},
    ],
)
def test_exponential_rejects_inverted_cap_missing_and_unknown_fields(
    policy: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        overlap_plan(
            at(0.0) | exponential(1.0, 4.0) | {"queue_retry_exponential": policy}
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"queue_retry_backoff_seconds": 1.0},
        {"queue_lease_seconds": None},
        {"max_queue_attempts": 1},
    ],
)
def test_exponential_is_exclusive_and_requires_scheduled_lease_and_retries(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        overlap_plan(at(0.0) | exponential(1.0, 4.0) | fields)
    raw = json.loads(plan().model_dump_json())
    raw["steps"][0].update(exponential(1.0, 4.0))
    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(raw))


def test_exponential_policy_is_normalized_and_omitted_when_unset() -> None:
    declared = overlap_plan(at(0.0) | exponential(1, 4)).steps[0]
    assert declared.model_dump()["queue_retry_exponential"] == {
        "initial_seconds": 1.0,
        "max_seconds": 4.0,
    }
    assert "queue_retry_exponential" not in overlap_plan(at(0.0)).steps[0].model_dump()


def test_independently_authored_capped_history_validates_without_scheduler_replay() -> (
    None
):
    history = Schedule.model_validate_json(
        json.dumps(
            {
                "version": 3,
                "peaks": {"active": 1, "queued": 1, "per_action": [["inspect", 1]]},
                "receipts": [
                    {
                        "submission": 1,
                        "action": "inspect",
                        "arrival_seconds": 0.0,
                        "admitted_seconds": 0.0,
                        "terminal_seconds": 6.0,
                        "outcome": "succeeded",
                    },
                    {
                        "submission": 2,
                        "action": "inspect",
                        "arrival_seconds": 0.0,
                        "admitted_seconds": 6.0,
                        "terminal_seconds": 6.5,
                        "outcome": "succeeded",
                        "attempts": [
                            {"enqueued_seconds": 0.0, "ended_seconds": 1.0},
                            {"enqueued_seconds": 1.5, "ended_seconds": 2.5},
                            {"enqueued_seconds": 3.5, "ended_seconds": 4.5},
                            {"enqueued_seconds": 6.0, "ended_seconds": 6.0},
                        ],
                        "backoffs": [
                            {"started_seconds": 1.0, "ended_seconds": 1.5},
                            {"started_seconds": 2.5, "ended_seconds": 3.5},
                            {"started_seconds": 4.5, "ended_seconds": 6.0},
                        ],
                    },
                ],
            }
        )
    )
    step = Step.model_validate_json(json.dumps(at(0.0, 0.5) | exponential(0.5, 1.5)))
    row = history.receipts[1]
    with patch(
        "scripts.fleet_harness.scheduler.Simulation.run",
        side_effect=AssertionError("replay"),
    ):
        assert lineage(row, step, 10.0)
        for update in (
            {"queue_retry_exponential": {"initial_seconds": 0.5, "max_seconds": 1.0}},
            {"queue_retry_exponential": {"initial_seconds": 0.25, "max_seconds": 1.5}},
            {"queue_retry_exponential": {"initial_seconds": 1.0, "max_seconds": 1.5}},
            {"queue_retry_exponential": None, "queue_retry_backoff_seconds": 1.5},
            {"queue_retry_exponential": None},
        ):
            other = Step.model_validate_json(
                json.dumps({**json.loads(step.model_dump_json()), **update})
            )
            assert not lineage(row, other, 10.0)
        assert not lineage(row, step, 6.0)
    assert history.version == 3
    with pytest.raises(ValidationError):
        Schedule.model_validate_json(
            history.model_dump_json().replace('"version":3', '"version":4')
        )


@pytest.mark.parametrize(
    "defect",
    [
        "cap",
        "initial",
        "delay",
        "deadline",
        "exhausted",
        "version_two",
        "version_one",
        "version_four",
        "policy_removed",
        "fixed_swapped",
        "both_policies",
        "missing",
        "extra",
        "peaks",
        "queue_sample",
    ],
)
def test_exponential_report_rejects_tampered_imports(defect: str) -> None:
    report = run(
        overlap_manifest(1, "operation_count", 2),
        overlap_plan(at(0.0, 2.5), at(0.0, 0.5) | exponential(0.25, 0.5, attempts=3)),
    )
    assert schedule(report).receipts[1].admitted_seconds == pytest.approx(2.75)
    assert RunReport.model_validate_json(report.model_dump_json()) == report
    raw = json.loads(report.model_dump_json())
    row = raw["schedule"]["receipts"][1]
    policy = raw["steps"][1]["queue_retry_exponential"]
    match defect:
        case "cap":
            policy["max_seconds"] = 0.25
        case "initial":
            policy["initial_seconds"] = 0.5
        case "delay":
            row["backoffs"][1]["ended_seconds"] = 2.7
            row["attempts"][2]["enqueued_seconds"] = 2.7
        case "deadline":
            raw["operation_timeout_seconds"] = 2.0
        case "exhausted":
            raw["steps"][1]["max_queue_attempts"] = 2
        case "version_two":
            raw["schedule"]["version"] = 2
        case "version_one":
            del raw["schedule"]["version"]
        case "version_four":
            raw["schedule"]["version"] = 4
        case "policy_removed":
            del raw["steps"][1]["queue_retry_exponential"]
        case "fixed_swapped":
            del raw["steps"][1]["queue_retry_exponential"]
            raw["steps"][1]["queue_retry_backoff_seconds"] = 0.25
        case "both_policies":
            raw["steps"][1]["queue_retry_backoff_seconds"] = 0.25
        case "missing":
            del row["backoffs"]
        case "extra":
            row["backoffs"].append({"started_seconds": 2.75, "ended_seconds": 3.0})
        case "peaks":
            raw["schedule"]["peaks"]["queued"] = 2
        case "queue_sample":
            next(
                s
                for s in raw["samples"]
                if s["submission"] == 2 and s["boundary"] == "queue_wait"
            )["seconds"] = 0.5
        case _:
            raise AssertionError(defect)
    with pytest.raises(ValidationError):
        RunReport.model_validate_json(json.dumps(raw))


def test_exponential_pending_terminal_cannot_outlive_another_cancellation() -> None:
    report = run(
        overlap_manifest(2, "operation_count", 4),
        overlap_plan(
            at(0.0, 2.0, outcome="failed"),
            at(0.0, 5.0),
            at(0.0) | exponential(8.0, 8.0, attempts=2),
            at(0.0) | exponential(8.0, 8.0, attempts=2),
            timeout=3.0,
        ),
    )
    assert receipts(report)[2:] == [(0.0, None, 2.0, "cancelled")] * 2
    raw = json.loads(report.model_dump_json())
    row = raw["schedule"]["receipts"][2]
    row.update(outcome="timed_out", terminal_seconds=3.0)
    row["backoffs"][-1]["ended_seconds"] = 3.0
    sample = next(s for s in raw["samples"] if s["submission"] == 3)
    sample.update(outcome="timed_out", seconds=3.0, ended_at_seconds=3.0)
    raw["outcomes"]["cancelled"] -= 1
    raw["outcomes"]["timed_out"] += 1
    with pytest.raises(ValidationError) as caught:
        RunReport.model_validate_json(json.dumps(raw))
    assert "schedule_times" in {error["type"] for error in caught.value.errors()}


def legacy_digest(steps: Plan) -> str:
    """Hash the pre-exponential operation keys independently of the runner."""
    keys = (
        "scenario",
        "endpoint",
        "action",
        "asset",
        "fault",
        "arrival_seconds",
        "queue_lease_seconds",
        "max_queue_attempts",
        "queue_retry_backoff_seconds",
    )
    kept = set(keys) - (set() if steps.scheduled else {"arrival_seconds"})
    dumped = [step.model_dump(include=kept) for step in steps.steps]
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(dumped, sort_keys=True).encode()).hexdigest()
    )


def test_exponential_digest_is_distinct_and_legacy_digests_are_unchanged() -> None:
    declaration = overlap_manifest(1, "operation_count", 1)
    legacy = (
        overlap_plan(at(0.0)),
        overlap_plan(at(0.0) | POLICY),
        overlap_plan(at(0.0) | POLICY | {"queue_retry_backoff_seconds": 2.0}),
    )
    for recipe in legacy:
        assert run(declaration, recipe).operation_digest == legacy_digest(recipe)
    sequential = plan()
    assert run(manifest(), sequential).operation_digest == legacy_digest(sequential)

    reports = [
        run(declaration, overlap_plan(at(0.0) | exponential(*policy)))
        for policy in ((0.5, 2.0), (0.5, 4.0), (1.0, 4.0))
    ]
    fixed = run(declaration, overlap_plan(at(0.0) | POLICY))
    operations = [
        {
            "scenario": "load",
            "endpoint": "example-local",
            "action": "inspect",
            "asset": None,
            "fault": None,
            "arrival_seconds": 0.0,
            **exponential(0.5, 2.0),
        }
    ]
    assert reports[0].operation_digest == (
        "sha256:"
        + hashlib.sha256(json.dumps(operations, sort_keys=True).encode()).hexdigest()
    )
    digests = {report.operation_digest for report in (*reports, fixed)}
    assert len(digests) == 4
    assert len({report.binding for report in (*reports, fixed)}) == 1
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
    assert (
        decision.decision == "no_go"
        and decision.connections == decision.candidate_launches == 0
    )
