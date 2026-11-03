from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier, Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionRejected,
    AdmissionRequest,
    ClassLimit,
    DemandSnapshot,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
)


@pytest.mark.parametrize("capacity", [None, 0, 1])
def test_pending_capacity_never_limits_immediate_calls_or_streams(
    capacity: int | None,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1, pending_demand_capacity=capacity)},
    )

    assert owner.request(pressure, demand=10).call(lambda: 7) == 7
    with owner.request(pressure, demand=10).stream(
        lambda: nullcontext(iter([8]))
    ) as stream:
        assert list(stream) == [8]

    snapshot = owner.snapshot().classes[0]
    assert snapshot.demand == DemandSnapshot(0, 0, 10, 0)
    assert snapshot.outcomes.completed == 2
    assert snapshot.outcomes.rejected_pending_demand_full == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_exact_fit_queue_rejects_overflow_and_reuses_cancelled_demand(
    streaming: bool,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 3, demand_capacity=10, pending_demand_capacity=5)},
    )
    stream = owner.request(pressure, demand=4).stream(lambda: nullcontext(iter(())))
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def work(request: AdmissionRequest) -> int:
        if streaming:
            with request.stream(lambda: nullcontext(iter([9]))) as output:
                return sum(output)
        return request.call(lambda: 9)

    with (
        ThreadPoolExecutor(1) as executor,
        patch.object(owner._condition, "wait", side_effect=signal_wait),
    ):
        try:
            request = owner.request(pressure, demand=5)
            result = executor.submit(work, request)
            assert queued.wait(3)
            assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 5, 4, 5)
            rejected = owner.request(pressure)
            with pytest.raises(AdmissionRejected) as raised:
                if streaming:
                    rejected.stream(lambda: pytest.fail("refused factory ran"))
                else:
                    rejected.call(lambda: pytest.fail("refused operation ran"))
            assert raised.value.reason is RejectionReason.PENDING_DEMAND_FULL
            assert not rejected.cancel()
            assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 5, 4, 5)

            assert request.cancel()
            with pytest.raises(AdmissionCancelled):
                result.result(3)
            assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 0, 4, 5)
            queued.clear()
            replacement = executor.submit(work, owner.request(pressure, demand=5))
            assert queued.wait(3)
            stream.close()
            assert replacement.result(3) == 9
        finally:
            owner.close()
            stream.close()

    snapshot = owner.snapshot().classes[0]
    assert (snapshot.active, snapshot.pending, snapshot.pending_high_water) == (0, 0, 1)
    assert snapshot.demand == DemandSnapshot(0, 0, 5, 5)
    assert snapshot.outcomes.cancelled == 1
    assert snapshot.outcomes.rejected_pending_demand_full == 1
    assert snapshot.outcomes.failed == snapshot.outcomes.rejected_full == 0
    assert snapshot.outcomes.completed == snapshot.outcomes.stream_closed == 1


def test_competing_enqueues_atomically_share_one_pending_demand_budget() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(3, 3, demand_capacity=10, pending_demand_capacity=3)},
    )
    stream = owner.request(pressure, demand=8).stream(lambda: nullcontext(iter(())))
    start, queued, refused = Barrier(3), Event(), Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def compete() -> RejectionReason:
        start.wait(3)
        try:
            owner.request(pressure, demand=3).call(
                lambda: pytest.fail("ungranted work ran")
            )
        except AdmissionRejected as error:
            refused.set()
            return error.reason
        pytest.fail("queued request unexpectedly returned")

    with (
        ThreadPoolExecutor(2) as executor,
        patch.object(owner._condition, "wait", side_effect=signal_wait),
    ):
        results = [executor.submit(compete) for _ in range(2)]
        try:
            start.wait(3)
            assert queued.wait(3) and refused.wait(3)
            snapshot = owner.snapshot().classes[0]
            assert (snapshot.active, snapshot.pending) == (1, 1)
            assert snapshot.demand == DemandSnapshot(8, 3, 8, 3)
            assert snapshot.outcomes.rejected_pending_demand_full == 1
            owner.close()
            assert {result.result(3) for result in results} == {
                RejectionReason.PENDING_DEMAND_FULL,
                RejectionReason.CLOSED,
            }
            assert owner.snapshot().classes[0].demand == DemandSnapshot(8, 0, 8, 3)
        finally:
            owner.close()
            stream.close()

    snapshot = owner.snapshot().classes[0]
    assert snapshot.demand == DemandSnapshot(0, 0, 8, 3)
    assert snapshot.outcomes.rejected_closed == 1
    assert snapshot.outcomes.rejected_pending_demand_full == 1
    assert snapshot.outcomes.stream_closed == 1
    assert snapshot.outcomes.failed == 0
