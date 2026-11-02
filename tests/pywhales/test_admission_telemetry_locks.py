from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


@pytest.mark.parametrize("cancel", [False, True])
def test_pending_decision_during_stream_close_releases_all_locks_before_observer(
    cancel: bool,
) -> None:
    records: list[TelemetryRecord] = []
    probes: list[bool] = []
    snapshots: list[tuple[int, int]] = []
    pressure = PressureClass("read")

    def observer(record: TelemetryRecord) -> None:
        records.append(record)
        if isinstance(record, AdmissionDecisionRecord) and record.outcome != "granted":
            try:
                probes.append(prober.submit(probe).result(3))
            except TimeoutError:
                probes.append(False)
            raise RuntimeError("private-observer-error")

    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=observer,
    )
    request = owner.request(pressure)

    @contextmanager
    def source() -> Generator[Iterator[int]]:
        try:
            yield iter(())
        finally:
            if cancel:
                assert request.cancel()
            else:
                owner.close()

    stream = owner.request(pressure).stream(source)

    def probe() -> bool:
        with stream._condition, owner._condition:
            snapshot = owner.snapshot().classes[0]
            snapshots.append((snapshot.active, snapshot.pending))
            return True

    queued = Event()
    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with (
        ThreadPoolExecutor(1) as prober,
        ThreadPoolExecutor(2) as workers,
        patch.object(owner._condition, "wait", side_effect=signal_wait),
    ):
        pending = workers.submit(request.call, lambda: pytest.fail("rejected work ran"))
        try:
            assert queued.wait(3)
            workers.submit(stream.close).result(3)
            with pytest.raises(AdmissionCancelled if cancel else AdmissionRejected):
                pending.result(3)
        finally:
            owner.close()
            stream.close()

    assert probes == [True]
    assert snapshots == [(1, 0)]
    decisions = [r for r in records if isinstance(r, AdmissionDecisionRecord)]
    assert [r.outcome for r in decisions] == [
        "granted",
        "cancelled" if cancel else "closed",
    ]
    assert len({r.admission_id for r in decisions}) == 2
    counts = owner.snapshot().classes[0]
    assert counts.active == counts.pending == 0
    assert counts.outcomes.cancelled == int(cancel)
    assert counts.outcomes.rejected_closed == int(not cancel)
    assert counts.outcomes.stream_closed == 1
    assert counts.outcomes.failed == 0


def test_grant_callback_is_unlocked_and_failure_preserves_result_and_release() -> None:
    records: list[TelemetryRecord] = []
    snapshots: list[int] = []
    pressure = PressureClass("read")

    def observer(record: TelemetryRecord) -> None:
        records.append(record)
        snapshots.append(
            prober.submit(lambda: owner.snapshot().classes[0].active).result(3)
        )
        raise ValueError("private-observer-failure")

    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=observer,
    )

    with ThreadPoolExecutor(1) as prober:
        assert owner.request(pressure).call(lambda: 7) == 7

    assert snapshots == [1]
    assert len(records) == 1
    counts = owner.snapshot().classes[0]
    assert (counts.active, counts.outcomes.completed, counts.outcomes.failed) == (
        0,
        1,
        0,
    )
