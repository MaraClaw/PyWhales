from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Event
from types import TracebackType
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
    SaturationPolicy,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord

FIRST, SECOND = PressureClass("private-first"), PressureClass("private-second")
REJECT = SaturationPolicy.REJECT


class Observer:
    """Collect decisions and probe the owner lock from another thread."""

    def __init__(self) -> None:
        self.records: list[AdmissionDecisionRecord] = []
        self.probes: list[int] = []
        self.owner: AdmissionOwner | None = None
        self.prober = ThreadPoolExecutor(1)

    def record(self, record: TelemetryRecord) -> None:
        if isinstance(record, AdmissionDecisionRecord):
            self.records.append(record)
            owner = self.owner
            assert owner is not None
            self.probes.append(
                self.prober.submit(lambda: owner.snapshot().classes[0].pending).result(
                    3
                )
            )
            raise ValueError("private observer failure")

    @property
    def outcomes(self) -> list[str]:
        return [record.outcome for record in self.records]


def owner_of(
    first: ClassLimit,
    second: ClassLimit,
    capacity: int = 1,
    observer: Observer | None = None,
) -> AdmissionOwner:
    return AdmissionOwner(
        EndpointIdentity("private-endpoint"),
        {FIRST: first, SECOND: second},
        telemetry_observer=None if observer is None else observer.record,
        owner_demand_capacity=capacity,
    )


@contextmanager
def queued_signal(owner: AdmissionOwner) -> Generator[Event]:
    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with patch.object(owner._condition, "wait", side_effect=signal_wait):
        yield queued


class GatedSource:
    """Source context whose exit and reads are released by the test."""

    def __init__(self, fail_exit: bool = False) -> None:
        self.read_started, self.release_read = Event(), Event()
        self.exit_started, self.release_exit = Event(), Event()
        self.fail_exit = fail_exit

    def __enter__(self) -> Iterator[int]:
        return self._read()

    def _read(self) -> Iterator[int]:
        self.read_started.set()
        assert self.release_read.wait(3)
        yield 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_started.set()
        assert self.release_exit.wait(3)
        if self.fail_exit:
            raise OSError("cleanup failed")


def refused(owner: AdmissionOwner) -> RejectionReason:
    with pytest.raises(AdmissionRejected) as raised:
        owner.request(SECOND).call(lambda: pytest.fail("refused work ran"))
    return raised.value.reason


@pytest.mark.parametrize("fail_exit", [False, True])
def test_stream_retains_owner_demand_through_source_exit_and_failed_cleanup(
    fail_exit: bool,
) -> None:
    owner = owner_of(ClassLimit(1, 0, REJECT), ClassLimit(1, 0, REJECT))
    source = GatedSource(fail_exit)
    stream = owner.request(FIRST).stream(lambda: source)
    with ThreadPoolExecutor(1) as executor:
        closer = executor.submit(stream.close)
        assert source.exit_started.wait(3)
        assert refused(owner) is RejectionReason.OWNER_DEMAND_FULL
        with pytest.raises(RuntimeError):
            stream.try_resize(1)
        source.release_exit.set()
        if fail_exit:
            with pytest.raises(OSError, match="cleanup failed"):
                closer.result(3)
        else:
            closer.result(3)
    assert owner.request(SECOND).call(lambda: 5) == 5
    assert owner.snapshot().classes[0].outcomes.failed == int(fail_exit)


def test_stream_retains_owner_demand_through_in_flight_read() -> None:
    owner = owner_of(ClassLimit(1, 0, REJECT), ClassLimit(1, 0, REJECT))
    source = GatedSource()
    stream = owner.request(FIRST).stream(lambda: source)
    source.release_exit.set()
    with ThreadPoolExecutor(2) as executor:
        reader = executor.submit(next, stream)
        assert source.read_started.wait(3)
        closer = executor.submit(stream.close)
        assert source.exit_started.wait(3)
        # Source exit has returned, but the read is still unwinding.
        assert refused(owner) is RejectionReason.OWNER_DEMAND_FULL
        assert not closer.done()
        source.release_read.set()
        with pytest.raises(StopIteration):
            reader.result(3)
        closer.result(3)
    assert owner.request(SECOND).call(lambda: 5) == 5


def test_refusals_commit_one_unlocked_payload_free_decision_each() -> None:
    observer = Observer()
    owner = owner_of(
        ClassLimit(1, 0, REJECT), ClassLimit(1, 0, REJECT), observer=observer
    )
    observer.owner = owner
    with owner.request(FIRST).stream(lambda: nullcontext(iter(()))):
        with pytest.raises(AdmissionRejected) as busy:
            owner.request(FIRST).call(lambda: pytest.fail("ran"))
        assert busy.value.reason is RejectionReason.BUSY
        assert refused(owner) is RejectionReason.OWNER_DEMAND_FULL
    observer.prober.shutdown()
    assert observer.outcomes == ["granted", "busy", "owner_demand_full"]
    assert observer.probes == [0, 0, 0]
    assert "private" not in repr(observer.records)
    assert owner.snapshot().classes[1].outcomes.rejected_owner_demand_full == 1


def test_waiting_grant_cancel_and_close_each_emit_exactly_once() -> None:
    observer = Observer()
    owner = owner_of(ClassLimit(1, 0), ClassLimit(1, 2), observer=observer)
    observer.owner = owner
    holder = owner.request(FIRST).stream(lambda: nullcontext(iter(())))
    granted, cancelled, closed = (owner.request(SECOND) for _ in range(3))
    try:
        with ThreadPoolExecutor(1) as executor:
            with queued_signal(owner) as queued:
                result = executor.submit(granted.call, lambda: 8)
                assert queued.wait(3)
            holder.close()
            assert result.result(3) == 8
            holder = owner.request(FIRST).stream(lambda: nullcontext(iter(())))
            with queued_signal(owner) as queued:
                doomed = executor.submit(cancelled.call, lambda: pytest.fail("ran"))
                assert queued.wait(3)
            assert cancelled.cancel()
            with pytest.raises(AdmissionCancelled):
                doomed.result(3)
            with queued_signal(owner) as queued:
                shut = executor.submit(closed.call, lambda: pytest.fail("ran"))
                assert queued.wait(3)
            owner.close()
            with pytest.raises(AdmissionRejected):
                shut.result(3)
    finally:
        holder.close()
        observer.prober.shutdown()
    assert observer.outcomes == ["granted", "granted", "granted", "cancelled", "closed"]
    assert observer.probes == [0] * 5


def test_disabled_telemetry_adds_no_identifier_or_clock_for_owner_refusal() -> None:
    owner = owner_of(ClassLimit(1, 0, REJECT), ClassLimit(1, 0, REJECT))
    with (
        patch("pywhales.admission._telemetry.uuid4", side_effect=AssertionError),
        patch("pywhales.admission._telemetry.monotonic", side_effect=AssertionError),
        patch("pywhales.admission._owner.monotonic", side_effect=AssertionError),
        owner.request(FIRST).stream(lambda: nullcontext(iter(()))),
    ):
        assert refused(owner) is RejectionReason.OWNER_DEMAND_FULL
