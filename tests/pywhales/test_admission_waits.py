from concurrent.futures import ThreadPoolExecutor
from threading import TIMEOUT_MAX, Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionOwner,
    AdmissionQueueTimeout,
    AdmissionReentryError,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


def test_interrupted_pending_wait_is_terminal_and_records_once() -> None:
    pressure = PressureClass("read")
    records: list[TelemetryRecord] = []
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=records.append,
    )
    bucket = owner._buckets[pressure]
    bucket.active = 1
    request = owner.request(pressure)
    error = KeyboardInterrupt()
    with (
        patch.object(owner._condition, "wait", side_effect=error),
        pytest.raises(KeyboardInterrupt) as raised,
    ):
        request.call(lambda: None)
    assert raised.value is error
    assert not request.cancel()
    snapshot = owner.snapshot().classes[0]
    assert snapshot.pending == 0
    assert (snapshot.outcomes.failed, snapshot.outcomes.cancelled) == (1, 0)
    assert len(records) == 1
    assert isinstance(records[0], AdmissionDecisionRecord)
    assert records[0].outcome == "failed"


def test_large_finite_queue_timeout_respects_platform_wait_limit() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    owner._buckets[pressure].active = 1
    request = owner.request(pressure, queue_timeout=1e100)
    waits: list[float | None] = []

    def interrupt(timeout: float | None = None) -> bool:
        waits.append(timeout)
        assert timeout is not None and timeout <= TIMEOUT_MAX
        raise KeyboardInterrupt

    with (
        patch.object(owner._condition, "wait", side_effect=interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        request.call(lambda: None)
    assert len(waits) == 1


def test_queue_deadline_covers_time_awaiting_admission_lock() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    clock_started = Event()
    now = [100.0]
    calls: list[int] = []

    def clock() -> float:
        value = now[0]
        clock_started.set()
        return value

    with (
        ThreadPoolExecutor(1) as executor,
        patch("pywhales.admission._owner.monotonic", side_effect=clock),
    ):
        with owner._condition:
            result = executor.submit(
                owner.request(pressure, 1).call, lambda: calls.append(1)
            )
            assert clock_started.wait(3)
            now[0] = 102.0
        with pytest.raises(AdmissionQueueTimeout):
            result.result(3)
    assert calls == []
    snapshot = owner.snapshot().classes[0]
    assert (snapshot.active, snapshot.pending, snapshot.outcomes.queue_timed_out) == (
        0,
        0,
        1,
    )


@pytest.mark.parametrize(
    "timeout",
    [None, 0, -1, -(10**400), True, float("inf"), float("-inf"), float("nan"), "1"],
)
def test_drain_rejects_invalid_timeout_before_waiting(timeout: float) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    with patch.object(owner._condition, "wait") as wait, pytest.raises(ValueError):
        owner.wait_drained(timeout=timeout)
    wait.assert_not_called()


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("timeout", [1, 10**400])
def test_empty_owner_drains_without_implicitly_closing(
    closed: bool, timeout: float
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    if closed:
        owner.close()

    assert owner.wait_drained(timeout=timeout)

    assert owner.snapshot().closed is closed


def test_drain_expiry_preserves_active_work() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    entered, release = Event(), Event()

    def held() -> int:
        entered.set()
        assert release.wait(3)
        return 7

    with ThreadPoolExecutor(1) as executor:
        result = executor.submit(owner.request(pressure).call, held)
        try:
            assert entered.wait(3)
            owner.close()

            assert not owner.wait_drained(timeout=0.001)

            assert not result.done()
            assert owner.snapshot().classes[0].active == 1
        finally:
            release.set()
        assert result.result(3) == 7
    assert owner.wait_drained(timeout=1)
    assert owner.snapshot().classes[0].outcomes.completed == 1


@pytest.mark.parametrize("timeout", [1e100, 10**400])
def test_large_drain_timeout_bounds_each_wait_and_rechecks_after_wakeup(
    timeout: float,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    bucket = owner._buckets[pressure]
    bucket.active = 1
    waits: list[float | None] = []

    def wake(timeout: float | None = None) -> bool:
        waits.append(timeout)
        assert timeout is not None and 0 < timeout <= TIMEOUT_MAX
        if len(waits) == 2:
            bucket.active = 0
        return True

    with patch.object(owner._condition, "wait", side_effect=wake):
        assert owner.wait_drained(timeout=timeout)
    assert waits == [TIMEOUT_MAX, TIMEOUT_MAX]


def test_drain_rejects_same_owner_callback_even_after_close() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    other = AdmissionOwner(EndpointIdentity("other"), {pressure: ClassLimit(1, 0)})

    def callback() -> None:
        owner.close()
        assert other.wait_drained(timeout=1)
        with pytest.raises(AdmissionReentryError):
            owner.wait_drained(timeout=1)

    owner.request(pressure).call(callback)
    assert owner.wait_drained(timeout=1)


@pytest.mark.parametrize("close_first", [False, True])
def test_close_and_queue_deadline_preserve_first_terminal_outcome(
    close_first: bool,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    owner._buckets[pressure].active = 1
    request = owner.request(pressure, queue_timeout=1)

    def wake(timeout: float | None = None) -> bool:
        if close_first:
            owner.close()
        owner._buckets[pressure].active = 0
        return True

    with (
        patch("pywhales.admission._owner.monotonic", side_effect=[100, 100, 101]),
        patch.object(owner._condition, "wait", side_effect=wake),
        pytest.raises(
            AdmissionRejected if close_first else AdmissionQueueTimeout
        ) as raised,
    ):
        request.call(lambda: pytest.fail("terminal callback ran"))
    owner.close()
    assert not request.cancel()
    if close_first:
        assert isinstance(raised.value, AdmissionRejected)
        assert raised.value.reason is RejectionReason.CLOSED
    counts = owner.snapshot().classes[0].outcomes
    assert (counts.rejected_closed, counts.queue_timed_out, counts.failed) == (
        int(close_first),
        int(not close_first),
        0,
    )


def test_drain_deadline_covers_time_awaiting_admission_lock() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    owner._buckets[pressure].active = 1
    started = Event()
    now = [100.0]

    def clock() -> float:
        value = now[0]
        started.set()
        return value

    with (
        ThreadPoolExecutor(1) as executor,
        patch("pywhales.admission._owner.monotonic", side_effect=clock),
    ):
        with owner._condition:
            result = executor.submit(owner.wait_drained, timeout=1)
            assert started.wait(3)
            now[0] = 102.0
        assert not result.result(3)
