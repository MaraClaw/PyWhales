from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier, Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionQueueTimeout,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
    SaturationPolicy,
)
from pywhales.admission._state import UINT64_MAX, Outcome
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


@pytest.mark.parametrize("capacity", [None, 0, 1])
def test_immediate_grants_ignore_owner_queue_limit(capacity: int | None) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        owner_pending_demand_capacity=capacity,
    )
    assert owner.request(pressure, demand=10).call(lambda: 7) == 7
    with owner.request(pressure, demand=10).stream(lambda: nullcontext(iter([8]))) as s:
        assert list(s) == [8]


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        (ClassLimit(1, 1, SaturationPolicy.REJECT), RejectionReason.BUSY),
        (ClassLimit(1, 0), RejectionReason.QUEUE_FULL),
        (
            ClassLimit(1, 1, pending_demand_capacity=0),
            RejectionReason.PENDING_DEMAND_FULL,
        ),
        (ClassLimit(1, 1), RejectionReason.OWNER_PENDING_DEMAND_FULL),
    ],
)
def test_owner_limit_preserves_rejection_precedence(
    limit: ClassLimit, reason: RejectionReason
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"), {pressure: limit}, owner_pending_demand_capacity=0
    )
    with (
        owner.request(pressure).stream(lambda: nullcontext(iter(()))),
        pytest.raises(AdmissionRejected) as raised,
    ):
        owner.request(pressure).call(lambda: pytest.fail("refused work ran"))
    assert raised.value.reason is reason


def test_competing_classes_share_exact_budget_and_cancellation_reuses_it() -> None:
    first, second = PressureClass("first"), PressureClass("second")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {first: ClassLimit(1, 2), second: ClassLimit(1, 2)},
        owner_pending_demand_capacity=3,
    )
    streams = [
        owner.request(p).stream(lambda: nullcontext(iter(()))) for p in (first, second)
    ]
    requests = [owner.request(p, demand=3) for p in (first, second)]
    start, queued, rejected = Barrier(3), Event(), Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def compete(index: int) -> str:
        start.wait(3)
        try:
            requests[index].call(lambda: pytest.fail("ungranted work ran"))
        except AdmissionRejected as error:
            assert error.reason is RejectionReason.OWNER_PENDING_DEMAND_FULL
            rejected.set()
            return "rejected"
        except AdmissionCancelled:
            return "cancelled"
        pytest.fail("ungranted work returned")

    with (
        ThreadPoolExecutor(2) as executor,
        patch.object(owner._condition, "wait", side_effect=signal_wait),
    ):
        futures = [executor.submit(compete, i) for i in range(2)]
        try:
            start.wait(3)
            assert queued.wait(3) and rejected.wait(3)
            assert sum(c.demand.pending for c in owner.snapshot().classes) == 3
            assert sum(request.cancel() for request in requests) == 1
            assert {future.result(3) for future in futures} == {"rejected", "cancelled"}
            queued.clear()
            replacement = owner.request(first, demand=3)
            result = executor.submit(replacement.call, lambda: 11)
            assert queued.wait(3)
            streams[0].close()
            assert result.result(3) == 11
            assert sum(c.demand.pending for c in owner.snapshot().classes) == 0
        finally:
            owner.close()
            for stream in streams:
                stream.close()
    assert (
        sum(
            c.outcomes.rejected_owner_pending_demand_full
            for c in owner.snapshot().classes
        )
        == 1
    )


def test_equal_endpoint_owners_have_independent_queue_budgets() -> None:
    pressure = PressureClass("read")
    owners = [
        AdmissionOwner(
            EndpointIdentity("local"),
            {pressure: ClassLimit(1, 1)},
            owner_pending_demand_capacity=1,
        )
        for _ in range(2)
    ]
    streams = [
        owner.request(pressure).stream(lambda: nullcontext(iter(())))
        for owner in owners
    ]
    with ThreadPoolExecutor(2) as executor:
        results = []
        try:
            for owner in owners:
                queued = Event()
                wait = owner._condition.wait

                def signal_wait(
                    timeout: float | None = None,
                    signal: Event = queued,
                    wait_for: Callable[[float | None], bool] = wait,
                ) -> bool:
                    signal.set()
                    return wait_for(timeout)

                with patch.object(owner._condition, "wait", side_effect=signal_wait):
                    results.append(
                        executor.submit(owner.request(pressure).call, lambda: 7)
                    )
                    assert queued.wait(3)
            assert [owner.snapshot().classes[0].demand.pending for owner in owners] == [
                1,
                1,
            ]
            for stream in streams:
                stream.close()
            assert [result.result(3) for result in results] == [7, 7]
        finally:
            for owner in owners:
                owner.close()
            for stream in streams:
                stream.close()


@pytest.mark.parametrize("terminal", ["timeout", "exception"])
def test_waiter_terminal_cleanup_releases_owner_budget(terminal: str) -> None:
    first, second = PressureClass("first"), PressureClass("second")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {first: ClassLimit(1, 1), second: ClassLimit(1, 1)},
        owner_pending_demand_capacity=1,
    )
    streams = [
        owner.request(p).stream(lambda: nullcontext(iter(()))) for p in (first, second)
    ]
    try:
        if terminal == "timeout":
            with (
                patch("pywhales.admission._owner.monotonic", side_effect=[0, 0, 1]),
                patch.object(owner._condition, "wait", return_value=True),
                pytest.raises(AdmissionQueueTimeout),
            ):
                owner.request(first, queue_timeout=1).call(lambda: None)
        else:
            with (
                patch.object(
                    owner._condition, "wait", side_effect=RuntimeError("wait failed")
                ),
                pytest.raises(RuntimeError, match="wait failed"),
            ):
                owner.request(first).call(lambda: None)
        queued = Event()
        wait = owner._condition.wait

        def signal_wait(timeout: float | None = None) -> bool:
            queued.set()
            return wait(timeout)

        with (
            ThreadPoolExecutor(1) as executor,
            patch.object(owner._condition, "wait", side_effect=signal_wait),
        ):
            result = executor.submit(owner.request(second).call, lambda: 42)
            assert queued.wait(3)
            streams[1].close()
            assert result.result(3) == 42
    finally:
        owner.close()
        for stream in streams:
            stream.close()


def test_owner_refusal_is_saturating_payload_free_and_unlocked() -> None:
    records: list[TelemetryRecord] = []
    probes: list[int] = []
    pressure = PressureClass("private-class")

    def observer(record: TelemetryRecord) -> None:
        records.append(record)
        if (
            isinstance(record, AdmissionDecisionRecord)
            and record.outcome == "owner_pending_demand_full"
        ):
            probes.append(
                prober.submit(lambda: owner.snapshot().classes[0].pending).result(3)
            )
            raise ValueError("private observer failure")

    owner = AdmissionOwner(
        EndpointIdentity("private-endpoint"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=observer,
        owner_pending_demand_capacity=0,
    )
    owner._buckets[pressure].counts[Outcome.REJECTED_OWNER_PENDING_DEMAND_FULL] = (
        UINT64_MAX - 1
    )
    with (
        ThreadPoolExecutor(1) as prober,
        owner.request(pressure).stream(lambda: nullcontext(iter(()))),
    ):
        for streaming in (False, True):
            with pytest.raises(AdmissionRejected) as raised:
                if streaming:
                    owner.request(pressure).stream(lambda: pytest.fail("factory ran"))
                else:
                    owner.request(pressure).call(lambda: pytest.fail("call ran"))
            assert raised.value.reason is RejectionReason.OWNER_PENDING_DEMAND_FULL
    decisions = [r for r in records if isinstance(r, AdmissionDecisionRecord)]
    assert [r.outcome for r in decisions] == [
        "granted",
        "owner_pending_demand_full",
        "owner_pending_demand_full",
    ]
    assert all(r.schema_version == 11 for r in decisions)
    assert "private" not in repr(decisions)
    assert probes == [0, 0]
    counts = owner.snapshot().classes[0].outcomes
    assert counts.rejected_owner_pending_demand_full == UINT64_MAX and counts.saturated
