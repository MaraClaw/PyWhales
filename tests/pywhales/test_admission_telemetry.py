import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, asdict
from threading import Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionQueueTimeout,
    AdmissionReentryError,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


def test_decisions_are_frozen_payload_free_and_reentry_fails_once() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("private-pressure")
    owner = AdmissionOwner(
        EndpointIdentity("private-endpoint"),
        {pressure: ClassLimit(1, 0, pending_demand_capacity=0)},
        telemetry_observer=records.append,
    )
    nested = owner.request(pressure)

    with (
        patch("pywhales.admission._telemetry.monotonic", side_effect=[10, 12, 20, 19]),
        pytest.raises(AdmissionReentryError),
    ):
        owner.request(pressure).call(lambda: nested.call(lambda: "private-callable"))
    with pytest.raises(RuntimeError):
        nested.call(lambda: None)

    assert len(records) == 2
    assert all(isinstance(record, AdmissionDecisionRecord) for record in records)
    first, second = records
    assert isinstance(first, AdmissionDecisionRecord)
    assert isinstance(second, AdmissionDecisionRecord)
    assert (first.outcome, second.outcome) == ("granted", "failed")
    assert (first.duration_seconds, second.duration_seconds) == (2, 0)
    assert first.admission_id != second.admission_id
    assert len(first.admission_id) == len(second.admission_id) == 32
    assert first.schema_version == second.schema_version == 11
    assert first.phase == second.phase == "admission"
    with pytest.raises(FrozenInstanceError):
        first.__setattr__("outcome", "failed")
    serialized = json.dumps([asdict(record) for record in records])
    assert "private" not in serialized
    counts = owner.snapshot().classes[0].outcomes
    assert counts.failed == 2


@pytest.mark.parametrize("queued", [False, True])
def test_queue_timeout_decides_once_before_any_work(queued: bool) -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=records.append,
    )
    owner._buckets[pressure].active = int(queued)
    request = owner.request(pressure, queue_timeout=1)

    with (
        patch("pywhales.admission._telemetry.monotonic", side_effect=[10, 13]),
        patch(
            "pywhales.admission._owner.monotonic",
            side_effect=[100, 100, 101] if queued else [100, 101],
        ),
        patch.object(owner._condition, "wait", return_value=True),
        pytest.raises(AdmissionQueueTimeout),
    ):
        request.call(lambda: pytest.fail("timed-out work ran"))
    assert not request.cancel()
    owner.close()

    assert len(records) == 1
    assert isinstance(records[0], AdmissionDecisionRecord)
    assert (records[0].outcome, records[0].duration_seconds) == ("queue_timeout", 3)
    assert owner.snapshot().classes[0].outcomes.queue_timed_out == 1


def test_activation_duration_includes_wait_for_owner_lock() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )
    started = Event()
    now = [10.0]

    def clock() -> float:
        value = now[0]
        started.set()
        return value

    with (
        ThreadPoolExecutor(1) as executor,
        patch("pywhales.admission._telemetry.monotonic", side_effect=clock),
    ):
        with owner._condition:
            result = executor.submit(owner.request(pressure).call, lambda: 7)
            assert started.wait(3)
            now[0] = 13
        assert result.result(3) == 7

    assert len(records) == 1
    assert isinstance(records[0], AdmissionDecisionRecord)
    assert (records[0].outcome, records[0].duration_seconds) == ("granted", 3)


def test_dormant_cancellation_never_gets_an_id_or_decision() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )
    request = owner.request(pressure)
    with patch("pywhales.admission._telemetry.uuid4", side_effect=AssertionError):
        assert request.cancel()
        owner.close()
        with pytest.raises(AdmissionCancelled):
            request.call(lambda: pytest.fail("cancelled work ran"))

    assert records == []
    assert owner.snapshot().classes[0].outcomes.cancelled == 1


def test_disabled_observation_adds_no_identifiers_clocks_or_callbacks() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        owner_pending_demand_capacity=0,
    )
    with (
        patch("pywhales.admission._telemetry.uuid4", side_effect=AssertionError),
        patch("pywhales.admission._telemetry.monotonic", side_effect=AssertionError),
        patch("pywhales.admission._owner.monotonic", side_effect=AssertionError),
        patch("pywhales.admission._telemetry.deliver", side_effect=AssertionError),
        patch("pywhales.admission._owner.deliver", side_effect=AssertionError),
    ):
        assert owner.request(pressure).call(lambda: 7) == 7
        with owner.request(pressure).stream(lambda: nullcontext(iter([3]))) as stream:
            with pytest.raises(AdmissionRejected):
                owner.request(pressure).call(lambda: pytest.fail("refused work ran"))
            assert list(stream) == [3]
        request = owner.request(pressure)
        assert request.cancel()
        owner.close()
        with pytest.raises(AdmissionRejected):
            owner.request(pressure).call(lambda: None)


def test_closed_dormant_request_emits_only_when_activated() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )
    request = owner.request(pressure)
    owner.close()
    assert records == []

    with pytest.raises(AdmissionRejected):
        request.call(lambda: pytest.fail("closed work ran"))
    with pytest.raises(RuntimeError):
        request.call(lambda: None)

    assert len(records) == 1
    assert isinstance(records[0], AdmissionDecisionRecord)
    assert records[0].outcome == "closed"
    assert owner.snapshot().classes[0].outcomes.rejected_closed == 1


def test_queued_grant_commits_once_after_release_and_before_work() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=records.append,
    )
    stream = owner.request(pressure).stream(lambda: nullcontext(iter(())))
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def work() -> int:
        assert len(records) == 2
        return 7

    with (
        ThreadPoolExecutor(1) as executor,
        patch.object(owner._condition, "wait", side_effect=signal_wait),
    ):
        result = executor.submit(owner.request(pressure).call, work)
        try:
            assert queued.wait(3)
            stream.close()
            assert result.result(3) == 7
        finally:
            stream.close()

    assert [r.outcome for r in records if isinstance(r, AdmissionDecisionRecord)] == [
        "granted",
        "granted",
    ]
    counts = owner.snapshot().classes[0]
    assert (counts.active, counts.pending, counts.outcomes.completed) == (0, 0, 1)
