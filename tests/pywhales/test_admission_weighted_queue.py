from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Event
from typing import Literal, assert_never
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
)


def test_shrink_wakes_fifo_head_without_allowing_growth_to_bypass_waiters() -> None:
    # Given
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(3, 2, demand_capacity=10, pending_demand_capacity=5)},
    )
    request = owner.request(pressure, demand=8)
    stream = request.stream(lambda: nullcontext(iter(())))
    release, head_started, tail_started = Event(), Event(), Event()
    order: list[int] = []
    wait = owner._condition.wait

    def hold(number: int, started: Event) -> None:
        order.append(number)
        started.set()
        assert release.wait(3)

    with ThreadPoolExecutor(2) as executor:
        try:
            for demand, started, number in ((4, head_started, 1), (1, tail_started, 2)):
                queued = Event()

                def signal_wait(
                    timeout: float | None = None, signal: Event = queued
                ) -> bool:
                    signal.set()
                    return wait(timeout)

                with patch.object(owner._condition, "wait", side_effect=signal_wait):
                    executor.submit(
                        owner.request(pressure, demand=demand).call,
                        lambda n=number, s=started: hold(n, s),
                    )
                    assert queued.wait(3)
            snapshot = owner.snapshot().classes[0]
            assert (snapshot.active, snapshot.pending, snapshot.pending_high_water) == (
                1,
                2,
                2,
            )
            assert snapshot.demand == DemandSnapshot(8, 5, 8, 5)
            assert request.try_resize(8)
            assert not request.try_resize(9)
            assert not head_started.is_set() and not tail_started.is_set()
            # When
            assert stream.try_resize(6)
            assert head_started.wait(3)
            # Then
            assert not tail_started.is_set()
            assert order == [1]
            assert owner.snapshot().classes[0].demand == DemandSnapshot(10, 1, 10, 5)
            assert request.try_resize(5)
            assert tail_started.wait(3)
            assert order == [1, 2]
            assert owner.snapshot().classes[0].demand == DemandSnapshot(10, 0, 10, 5)
        finally:
            release.set()
            stream.close()
    assert owner.wait_drained(timeout=1)
    snapshot = owner.snapshot().classes[0]
    assert snapshot.demand == DemandSnapshot(0, 0, 10, 5)
    assert snapshot.outcomes.completed == 2


def test_pending_request_refuses_growth_even_with_unbounded_demand() -> None:
    # Given
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    request = owner.request(pressure, demand=4)
    stream = request.stream(lambda: nullcontext(iter(())))
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with ThreadPoolExecutor(1) as executor:
        try:
            with patch.object(owner._condition, "wait", side_effect=signal_wait):
                pending = owner.request(pressure, demand=9)
                result = executor.submit(pending.call, lambda: 7)
                assert queued.wait(3)
            # When / Then
            assert not request.try_resize(5)
            with pytest.raises(RuntimeError):
                pending.try_resize(1)
            assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 9, 4, 9)
        finally:
            stream.close()
        assert result.result(3) == 7
    assert owner.snapshot().classes[0].demand == DemandSnapshot(0, 0, 9, 9)


@pytest.mark.parametrize("terminal", ["cancel", "timeout", "close"])
def test_queue_terminal_paths_drop_demand_without_releasing_active_work(
    terminal: Literal["cancel", "timeout", "close"],
) -> None:
    # Given
    pressure = PressureClass("read")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(2, 1, demand_capacity=5, pending_demand_capacity=3)},
    )
    active = owner.request(pressure, demand=4)
    stream = active.stream(lambda: nullcontext(iter(())))
    request = owner.request(pressure, 1 if terminal == "timeout" else None, demand=3)
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with ThreadPoolExecutor(1) as executor:
        try:
            with (
                patch("pywhales.admission._owner.monotonic", return_value=100) as clock,
                patch.object(owner._condition, "wait", side_effect=signal_wait),
            ):
                result = executor.submit(
                    request.call, lambda: pytest.fail("terminal work ran")
                )
                assert queued.wait(3)
                assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 3, 4, 3)
                # When / Then
                match terminal:
                    case "cancel":
                        assert request.cancel()
                        with pytest.raises(AdmissionCancelled):
                            result.result(3)
                    case "timeout":
                        with owner._condition:
                            clock.return_value = 102
                            owner._condition.notify_all()
                        with pytest.raises(AdmissionQueueTimeout):
                            result.result(3)
                    case "close":
                        owner.close()
                        with pytest.raises(AdmissionRejected) as raised:
                            result.result(3)
                        assert raised.value.reason is RejectionReason.CLOSED
                    case unreachable:
                        assert_never(unreachable)
            assert not request.cancel()
            assert not owner.wait_drained(timeout=0.001)
            assert owner.snapshot().classes[0].demand == DemandSnapshot(4, 0, 4, 3)
            assert active.try_resize(5)
            with pytest.raises(RuntimeError):
                request.try_resize(1)
        finally:
            stream.close()
    assert owner.wait_drained(timeout=1)
    snapshot = owner.snapshot().classes[0]
    assert snapshot.active == snapshot.pending == 0
    assert snapshot.demand == DemandSnapshot(0, 0, 5, 3)
    counts = snapshot.outcomes
    assert (
        counts.cancelled,
        counts.queue_timed_out,
        counts.rejected_closed,
        counts.failed,
    ) == (
        int(terminal == "cancel"),
        int(terminal == "timeout"),
        int(terminal == "close"),
        0,
    )
