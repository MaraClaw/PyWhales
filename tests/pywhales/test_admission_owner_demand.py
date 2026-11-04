from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionQueueTimeout,
    AdmissionRejected,
    ClassLimit,
    DemandSnapshot,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
    SaturationPolicy,
)
from pywhales.admission._state import UINT64_MAX, Outcome

FIRST, SECOND = PressureClass("first"), PressureClass("second")


@contextmanager
def queued_signal(owner: AdmissionOwner) -> Generator[Event]:
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with patch.object(owner._condition, "wait", side_effect=signal_wait):
        yield queued


def owner_of(
    capacity: int | None, first: ClassLimit, second: ClassLimit
) -> AdmissionOwner:
    return AdmissionOwner(
        EndpointIdentity("local"),
        {FIRST: first, SECOND: second},
        owner_demand_capacity=capacity,
    )


def active_demand(owner: AdmissionOwner) -> list[int]:
    return [state.demand.active for state in owner.snapshot().classes]


@pytest.mark.parametrize("capacity", [0, -1, True, False, 1.5, "2"])
def test_owner_rejects_invalid_demand_capacity(capacity: int) -> None:
    with pytest.raises(ValueError):
        AdmissionOwner(
            EndpointIdentity("local"),
            {FIRST: ClassLimit(1, 0)},
            owner_demand_capacity=capacity,
        )


def test_impossible_initial_and_resized_demand_is_rejected_before_activation() -> None:
    owner = owner_of(3, ClassLimit(2, 0), ClassLimit(2, 0, demand_capacity=2))
    with pytest.raises(ValueError, match="owner demand capacity"):
        owner.request(FIRST, demand=4)
    with pytest.raises(ValueError, match="class demand capacity"):
        owner.request(SECOND, demand=3)
    request = owner.request(FIRST, demand=3)
    with request.stream(lambda: nullcontext(iter(()))) as stream:
        before = owner.snapshot()
        with pytest.raises(ValueError, match="owner demand capacity"):
            stream.try_resize(4)
        assert owner.snapshot() == before
    assert active_demand(owner) == [0, 0]


def test_unset_capacity_keeps_classes_independent_and_growth_unblocked() -> None:
    owner = owner_of(None, ClassLimit(2, 1), ClassLimit(1, 1, demand_capacity=1))
    with (
        owner.request(FIRST, demand=10**30).stream(lambda: nullcontext(iter(()))) as a,
        owner.request(SECOND).stream(lambda: nullcontext(iter(()))),
        ThreadPoolExecutor(1) as executor,
        queued_signal(owner) as queued,
    ):
        waiter = executor.submit(owner.request(SECOND).call, lambda: 1)
        assert queued.wait(3)
        assert a.try_resize(10**31)
        assert owner.request(FIRST, demand=10**40).call(lambda: 5) == 5
        owner.close()
        with pytest.raises(AdmissionRejected):
            waiter.result(3)


def test_reject_class_fast_grant_is_atomic_exact_and_precedence_ordered() -> None:
    reject = SaturationPolicy.REJECT
    owner = owner_of(3, ClassLimit(1, 0, reject), ClassLimit(2, 0, reject))
    with owner.request(FIRST, demand=2).stream(lambda: nullcontext(iter(()))):
        with pytest.raises(AdmissionRejected) as raised:
            owner.request(SECOND, demand=2).call(lambda: pytest.fail("ran"))
        assert raised.value.reason is RejectionReason.OWNER_DEMAND_FULL
        assert active_demand(owner) == [2, 0]
        # Class occupancy outranks the owner reason once the class is itself full.
        with pytest.raises(AdmissionRejected) as busy:
            owner.request(FIRST).call(lambda: pytest.fail("ran"))
        assert busy.value.reason is RejectionReason.BUSY
        assert owner.request(SECOND, demand=1).call(lambda: active_demand(owner)) == [
            2,
            1,
        ]
    counts = [state.outcomes for state in owner.snapshot().classes]
    assert [c.rejected_owner_demand_full for c in counts] == [0, 1]
    assert [c.rejected_busy for c in counts] == [1, 0]
    assert active_demand(owner) == [0, 0]


def test_class_demand_capacity_outranks_owner_reason() -> None:
    reject = SaturationPolicy.REJECT
    owner = owner_of(
        2, ClassLimit(2, 0, reject, demand_capacity=1), ClassLimit(1, 0, reject)
    )
    with (
        owner.request(FIRST).stream(lambda: nullcontext(iter(()))),
        owner.request(SECOND).stream(lambda: nullcontext(iter(()))),
        pytest.raises(AdmissionRejected) as raised,
    ):
        owner.request(FIRST).call(lambda: pytest.fail("ran"))
    assert raised.value.reason is RejectionReason.BUSY


def test_wait_requests_keep_queue_limits_before_owner_demand() -> None:
    owner = owner_of(
        1,
        ClassLimit(1, 0),
        ClassLimit(1, 1, pending_demand_capacity=0),
    )
    with owner.request(FIRST).stream(lambda: nullcontext(iter(()))):
        for pressure, reason in (
            (FIRST, RejectionReason.QUEUE_FULL),
            (SECOND, RejectionReason.PENDING_DEMAND_FULL),
        ):
            with pytest.raises(AdmissionRejected) as raised:
                owner.request(pressure).call(lambda: pytest.fail("ran"))
            assert raised.value.reason is reason
    assert all(
        state.outcomes.rejected_owner_demand_full == 0
        for state in owner.snapshot().classes
    )


def test_queued_grant_waits_for_other_class_release_then_reuses_exact_fit() -> None:
    owner = owner_of(2, ClassLimit(1, 1), ClassLimit(1, 1))
    holder = owner.request(FIRST, demand=2).stream(lambda: nullcontext(iter(())))
    try:
        with ThreadPoolExecutor(1) as executor, queued_signal(owner) as queued:
            result = executor.submit(
                owner.request(SECOND, demand=2).call, lambda: active_demand(owner)
            )
            assert queued.wait(3)
            assert [(s.active, s.pending) for s in owner.snapshot().classes] == [
                (1, 0),
                (0, 1),
            ]
            holder.close()
            assert result.result(3) == [0, 2]
    finally:
        holder.close()
    assert active_demand(owner) == [0, 0]


def test_head_only_fifo_survives_owner_blocking_and_shrink_wakes_head() -> None:
    owner = owner_of(3, ClassLimit(2, 2), ClassLimit(1, 0))
    holder = owner.request(SECOND, demand=2).stream(lambda: nullcontext(iter(())))
    order: list[str] = []

    def work(name: str) -> str:
        order.append(name)
        return name

    try:
        with ThreadPoolExecutor(2) as executor:
            futures = []
            for name, demand in (("head", 2), ("follower", 1)):
                with queued_signal(owner) as queued:
                    futures.append(
                        executor.submit(
                            owner.request(FIRST, demand=demand).call,
                            lambda name=name: work(name),
                        )
                    )
                    assert queued.wait(3)
            # The follower would fit the owner allowance but cannot pass its head.
            assert order == []
            assert owner.snapshot().classes[0].pending == 2
            assert holder.try_resize(1)
            assert [future.result(3) for future in futures] == ["head", "follower"]
    finally:
        holder.close()
    assert order == ["head", "follower"]


def test_failed_growth_is_atomic_and_shrink_or_release_wakes_other_class() -> None:
    owner = owner_of(3, ClassLimit(1, 0), ClassLimit(2, 1))
    first = owner.request(FIRST, demand=2).stream(lambda: nullcontext(iter(())))
    second = owner.request(SECOND, demand=1).stream(lambda: nullcontext(iter(())))
    try:
        before = owner.snapshot()
        assert not first.try_resize(3)
        assert not second.try_resize(2)
        assert owner.snapshot() == before
        assert second.try_resize(1)
        with ThreadPoolExecutor(1) as executor, queued_signal(owner) as queued:
            waiter = executor.submit(owner.request(SECOND, demand=1).call, lambda: 9)
            assert queued.wait(3)
            assert not first.try_resize(3)
            assert second.try_resize(1)
            assert first.try_resize(1)
            assert waiter.result(3) == 9
        assert first.try_resize(2)
        assert owner.snapshot().classes[0].demand == DemandSnapshot(2, 0, 2, 0)
    finally:
        first.close()
        second.close()
    assert active_demand(owner) == [0, 0]


@pytest.mark.parametrize(("capacity", "grows"), [(10, False), (None, True)])
def test_enabled_capacity_stops_growth_from_bypassing_any_classs_queue(
    capacity: int | None, grows: bool
) -> None:
    owner = owner_of(capacity, ClassLimit(3, 0), ClassLimit(3, 1, demand_capacity=1))
    grower = owner.request(FIRST).stream(lambda: nullcontext(iter(())))
    blocker = owner.request(SECOND).stream(lambda: nullcontext(iter(())))
    try:
        with ThreadPoolExecutor(1) as executor, queued_signal(owner) as queued:
            waiter = executor.submit(owner.request(SECOND).call, lambda: 4)
            assert queued.wait(3)
            assert grower.try_resize(2) is grows
            assert active_demand(owner) == [2 if grows else 1, 1]
            blocker.close()
            assert waiter.result(3) == 4
        assert grower.try_resize(2)
    finally:
        grower.close()
        blocker.close()


def test_cancellation_and_timeout_leave_active_owner_demand_untouched() -> None:
    owner = owner_of(1, ClassLimit(1, 2), ClassLimit(1, 2))
    holder = owner.request(FIRST).stream(lambda: nullcontext(iter(())))
    cancelled = owner.request(SECOND)
    try:
        with ThreadPoolExecutor(1) as executor, queued_signal(owner) as queued:
            result = executor.submit(cancelled.call, lambda: pytest.fail("ran"))
            assert queued.wait(3)
            assert cancelled.cancel()
            with pytest.raises(AdmissionCancelled):
                result.result(3)
        with (
            patch("pywhales.admission._owner.monotonic", side_effect=[0, 0, 1]),
            patch.object(owner._condition, "wait", return_value=True),
            pytest.raises(AdmissionQueueTimeout),
        ):
            owner.request(SECOND, queue_timeout=1).call(lambda: None)
        assert active_demand(owner) == [1, 0]
        assert [state.demand.pending for state in owner.snapshot().classes] == [0, 0]
    finally:
        holder.close()


def test_owner_close_rejects_waiters_but_keeps_active_demand_reserved() -> None:
    owner = owner_of(1, ClassLimit(1, 1), ClassLimit(1, 1))
    holder = owner.request(FIRST).stream(lambda: nullcontext(iter(())))
    try:
        with ThreadPoolExecutor(1) as executor, queued_signal(owner) as queued:
            result = executor.submit(owner.request(SECOND).call, lambda: None)
            assert queued.wait(3)
            owner.close()
            with pytest.raises(AdmissionRejected) as raised:
                result.result(3)
            assert raised.value.reason is RejectionReason.CLOSED
        assert active_demand(owner) == [1, 0]
        assert not owner.wait_drained(timeout=0.01)
    finally:
        holder.close()
    assert owner.wait_drained(timeout=1)


def test_owner_demand_counter_saturates_and_owners_stay_independent() -> None:
    reject = SaturationPolicy.REJECT
    owners = [
        owner_of(1, ClassLimit(1, 0, reject), ClassLimit(1, 0, reject))
        for _ in range(2)
    ]
    owners[0]._buckets[SECOND].counts[Outcome.REJECTED_OWNER_DEMAND_FULL] = (
        UINT64_MAX - 1
    )
    with owners[0].request(FIRST).stream(lambda: nullcontext(iter(()))):
        for _ in range(2):
            with pytest.raises(AdmissionRejected):
                owners[0].request(SECOND).call(lambda: pytest.fail("ran"))
        assert owners[1].request(SECOND).call(lambda: 3) == 3
    counts = owners[0].snapshot().classes[1].outcomes
    assert counts.rejected_owner_demand_full == UINT64_MAX and counts.saturated
    assert owners[1].snapshot().classes[1].outcomes.rejected_owner_demand_full == 0
