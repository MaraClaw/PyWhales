from collections.abc import Callable
from queue import Queue
from threading import Event, Thread
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionQueueTimeout,
    AdmissionReentryError,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
    SaturationPolicy,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


def _spawn[T](operation: Callable[[], T]) -> tuple[Thread, Queue[T | BaseException]]:
    output: Queue[T | BaseException] = Queue()

    def run() -> None:
        try:
            output.put(operation())
        except BaseException as error:
            output.put(error)

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread, output


@pytest.mark.parametrize(
    "policy,pending,reason",
    [
        (SaturationPolicy.REJECT, 10, RejectionReason.BUSY),
        (SaturationPolicy.REJECT, 0, RejectionReason.BUSY),
        (SaturationPolicy.WAIT, 0, RejectionReason.QUEUE_FULL),
        (SaturationPolicy.WAIT, 10, RejectionReason.PENDING_DEMAND_FULL),
    ],
)
def test_busy_rejection_invokes_zero_work(
    policy: SaturationPolicy, pending: int, reason: RejectionReason
) -> None:
    pressure = PressureClass("create")
    records: list[TelemetryRecord] = []
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, pending, policy, pending_demand_capacity=0)},
        telemetry_observer=records.append,
    )
    active, release = Event(), Event()

    def held() -> None:
        active.set()
        assert release.wait(3)

    thread, output = _spawn(lambda: owner.request(pressure).call(held))
    try:
        assert active.wait(3)
        client = DockerClient()
        with patch(
            "pywhales.components.image.cli_wrapper.ImageCLI._pull_if_necessary"
        ) as preflight:
            with pytest.raises(AdmissionRejected) as raised:
                owner.request(pressure).call(
                    lambda: client.create("credential-payload")
                )
            assert raised.value.reason is reason
            preflight.assert_not_called()
        assert owner.snapshot().classes[0].pending == 0
        counts = owner.snapshot().classes[0].outcomes
        assert (
            counts.rejected_busy,
            counts.rejected_full,
            counts.rejected_pending_demand_full,
        ) == (
            int(reason is RejectionReason.BUSY),
            int(reason is RejectionReason.QUEUE_FULL),
            int(reason is RejectionReason.PENDING_DEMAND_FULL),
        )
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert output.get(timeout=3) is None
    assert all(isinstance(record, AdmissionDecisionRecord) for record in records)
    assert [
        record.outcome
        for record in records
        if isinstance(record, AdmissionDecisionRecord)
    ] == ["granted", reason.value]


def test_fifo_queue_is_bounded_and_dormant_handles_are_not_registered() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 2)})
    first_active, release = Event(), Event()
    order: list[int] = []

    def held() -> None:
        first_active.set()
        assert release.wait(3)

    first, output = _spawn(lambda: owner.request(pressure).call(held))
    workers: list[Thread] = []
    results: list[Queue[int | BaseException]] = []
    wait = owner._condition.wait
    try:
        assert first_active.wait(3)
        for number in range(2):
            queued = Event()

            def signal_wait(
                timeout: float | None = None, signal: Event = queued
            ) -> bool:
                signal.set()
                return wait(timeout)

            def record(value: int = number) -> int:
                order.append(value)
                return value

            with patch.object(owner._condition, "wait", side_effect=signal_wait):
                thread, result = _spawn(
                    lambda operation=record: owner.request(pressure).call(operation)
                )
                workers.append(thread)
                results.append(result)
                assert queued.wait(3)
        dormant = [owner.request(pressure) for _ in range(100)]
        assert len(dormant) == 100
        assert owner.snapshot().classes[0].pending == 2
        with pytest.raises(AdmissionRejected) as raised:
            owner.request(pressure).call(lambda: order.append(99))
        assert raised.value.reason is RejectionReason.QUEUE_FULL
    finally:
        release.set()
        first.join(3)
        for thread in workers:
            thread.join(3)
    assert not first.is_alive() and all(not thread.is_alive() for thread in workers)
    assert output.get(timeout=3) is None
    assert [result.get(timeout=3) for result in results] == [0, 1]
    snapshot = owner.snapshot().classes[0]
    assert order == [0, 1]
    assert (
        snapshot.active,
        snapshot.pending,
        snapshot.active_high_water,
        snapshot.pending_high_water,
    ) == (0, 0, 1, 2)


def test_queued_cancel_removes_ticket_and_invokes_zero_work() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    active, release, queued = Event(), Event(), Event()
    request = owner.request(pressure)

    def held() -> None:
        active.set()
        assert release.wait(3)

    first, first_output = _spawn(lambda: owner.request(pressure).call(held))
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    calls: list[str] = []
    try:
        assert active.wait(3)
        with patch.object(owner._condition, "wait", side_effect=signal_wait):
            second, second_output = _spawn(
                lambda: request.call(lambda: calls.append("preflight"))
            )
            assert queued.wait(3)
            assert request.cancel()
            assert not request.cancel()
            second.join(3)
        assert not second.is_alive()
        assert isinstance(second_output.get(timeout=3), AdmissionCancelled)
        assert calls == []
        assert owner.snapshot().classes[0].pending == 0
    finally:
        release.set()
        first.join(3)
    assert first_output.get(timeout=3) is None
    assert owner.snapshot().classes[0].outcomes.cancelled == 1


def test_queue_deadline_precedes_grant_at_release_boundary() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    bucket = owner._buckets[pressure]
    calls: list[str] = []
    request = owner.request(pressure, queue_timeout=1)
    bucket.active = 1

    def release_at_expiry(timeout: float | None = None) -> bool:
        bucket.active = 0
        return True

    with (
        patch("pywhales.admission._owner.monotonic", side_effect=[100, 100, 101]),
        patch.object(owner._condition, "wait", side_effect=release_at_expiry),
        pytest.raises(AdmissionQueueTimeout),
    ):
        request.call(lambda: calls.append("preflight"))
    assert calls == []
    snapshot = owner.snapshot().classes[0]
    assert (snapshot.active, snapshot.pending, snapshot.outcomes.queue_timed_out) == (
        0,
        0,
        1,
    )
    assert not request.cancel()


@pytest.mark.parametrize(
    "error", [ValueError("original"), KeyboardInterrupt(), SystemExit(7)]
)
def test_call_releases_on_baseexception_and_preserves_identity(
    error: BaseException,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    request = owner.request(pressure)

    def fail() -> None:
        assert not request.cancel()
        raise error

    with pytest.raises(type(error)) as raised:
        request.call(fail)
    assert raised.value is error
    assert not request.cancel()
    with pytest.raises(RuntimeError):
        request.call(lambda: 3)
    assert owner.request(pressure).call(lambda: 2) == 2
    assert owner.snapshot().classes[0].outcomes.failed == 1


def test_reentry_rejects_same_owner_across_classes() -> None:
    read, write = PressureClass("read"), PressureClass("write")
    owner = AdmissionOwner(
        EndpointIdentity("local"), {read: ClassLimit(1, 0), write: ClassLimit(1, 0)}
    )
    with pytest.raises(AdmissionReentryError):
        owner.request(read).call(lambda: owner.request(write).call(lambda: None))
    assert owner.snapshot().classes[0].active == 0


def test_cancel_before_use_is_once_only_and_request_stays_single_use() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    request = owner.request(pressure)
    assert request.cancel()
    with pytest.raises(AdmissionCancelled):
        request.call(lambda: 1)
    with pytest.raises(RuntimeError):
        request.call(lambda: 1)
    assert not request.cancel()


@pytest.mark.parametrize("cancel_first", [False, True])
def test_dormant_request_observes_close_without_replacing_cancellation(
    cancel_first: bool,
) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    request = owner.request(pressure)
    calls: list[int] = []
    if cancel_first:
        assert request.cancel()

    owner.close()

    assert not request.cancel()
    with pytest.raises(
        AdmissionCancelled if cancel_first else AdmissionRejected
    ) as raised:
        request.call(lambda: calls.append(1))
    if not cancel_first:
        assert isinstance(raised.value, AdmissionRejected)
        assert raised.value.reason is RejectionReason.CLOSED
    with pytest.raises(RuntimeError):
        request.call(lambda: calls.append(2))
    assert calls == []
    counts = owner.snapshot().classes[0].outcomes
    assert (counts.cancelled, counts.rejected_closed) == (
        int(cancel_first),
        int(not cancel_first),
    )
