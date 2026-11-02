from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Barrier, Event, current_thread
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.admission import (
    AdmissionCancelled,
    AdmissionOwner,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
    RejectionReason,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


def test_concurrent_closers_wait_for_one_exit_and_inflight_next() -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    reading, exiting, finish_exit, finish_read = Event(), Event(), Event(), Event()
    reader_woken, first_waiting, second_waiting = Event(), Event(), Event()
    exits: list[int] = []

    def records() -> Iterator[int]:
        reading.set()
        assert exiting.wait(3)
        reader_woken.set()
        assert finish_read.wait(3)
        yield 7

    @contextmanager
    def source() -> Generator[Iterator[int]]:
        try:
            yield records()
        finally:
            exits.append(1)
            exiting.set()
            assert finish_exit.wait(3)

    stream = owner.request(pressure).stream(source)
    wait = stream._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        if current_thread().name.startswith("first"):
            first_waiting.set()
        if current_thread().name.startswith("second"):
            second_waiting.set()
        return wait(timeout)

    with (
        ThreadPoolExecutor(1, "reader") as reader,
        ThreadPoolExecutor(1, "first") as first,
        ThreadPoolExecutor(1, "second") as second,
        patch.object(stream._condition, "wait", side_effect=signal_wait),
    ):
        read = reader.submit(next, stream)
        try:
            assert reading.wait(3)
            close = first.submit(stream.close)
            assert exiting.wait(3) and reader_woken.wait(3)
            duplicate = second.submit(stream.close)
            assert second_waiting.wait(3)
            assert owner.snapshot().classes[0].active == 1
            finish_exit.set()
            assert first_waiting.wait(3)
            assert owner.snapshot().classes[0].active == 1
            assert not close.done() and not duplicate.done()
        finally:
            finish_exit.set()
            finish_read.set()
        close.result(3)
        duplicate.result(3)
        with pytest.raises(StopIteration):
            read.result(3)
    assert exits == [1]
    assert owner.snapshot().classes[0].active == 0
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1


@pytest.mark.parametrize("_repetition", range(20))
def test_cancel_races_grant_atomically_without_starting_cancelled_preflight(
    _repetition: int,
) -> None:
    pressure = PressureClass("create")
    records: list[TelemetryRecord] = []
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1)},
        telemetry_observer=records.append,
    )
    held, release, queued = Event(), Event(), Event()
    race = Barrier(2)
    calls: list[int] = []
    request = owner.request(pressure)

    def active() -> None:
        held.set()
        assert release.wait(3)
        race.wait(3)

    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def cancel() -> bool:
        race.wait(3)
        return request.cancel()

    with ThreadPoolExecutor(3) as executor:
        first = executor.submit(owner.request(pressure).call, active)
        try:
            assert held.wait(3)
            with patch.object(owner._condition, "wait", side_effect=signal_wait):
                second = executor.submit(request.call, lambda: calls.append(1))
                assert queued.wait(3)
                cancelled = executor.submit(cancel)
                release.set()
                won = cancelled.result(3)
                if won:
                    with pytest.raises(AdmissionCancelled):
                        second.result(3)
                    assert calls == []
                else:
                    second.result(3)
                    assert calls == [1]
        finally:
            release.set()
        first.result(3)
    assert owner.snapshot().classes[0].active == 0
    assert all(isinstance(record, AdmissionDecisionRecord) for record in records)
    assert [
        record.outcome
        for record in records
        if isinstance(record, AdmissionDecisionRecord)
    ] == ["granted", "cancelled" if won else "granted"]


def test_cancelled_real_create_never_performs_pull_preflight_or_launch() -> None:
    pressure = PressureClass("create")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    request = owner.request(pressure)
    client = DockerClient()
    assert request.cancel()
    with (
        patch(
            "pywhales.components.image.cli_wrapper.ImageCLI._pull_if_necessary"
        ) as preflight,
        patch("pywhales.components.container.cli_wrapper.run") as launch,
    ):
        with pytest.raises(AdmissionCancelled):
            request.call(lambda: client.create("private-image"))
        preflight.assert_not_called()
        launch.assert_not_called()


@pytest.mark.parametrize("cancel_first", [False, True])
def test_close_wakes_pending_callers_and_serializes_cancellation(
    cancel_first: bool,
) -> None:
    pressures = (PressureClass("read"), PressureClass("write"))
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 1) for pressure in pressures},
    )
    streams = [
        owner.request(pressure).stream(lambda: nullcontext(iter(())))
        for pressure in pressures
    ]
    requests = [owner.request(pressure) for pressure in pressures]
    wait = owner._condition.wait

    with ThreadPoolExecutor(2) as executor:
        results = []
        try:
            for request in requests:
                queued = Event()

                def signal_wait(
                    timeout: float | None = None, signal: Event = queued
                ) -> bool:
                    signal.set()
                    return wait(timeout)

                with patch.object(owner._condition, "wait", side_effect=signal_wait):
                    results.append(
                        executor.submit(
                            request.call, lambda: pytest.fail("closed callback ran")
                        )
                    )
                    assert queued.wait(3)
            with owner._condition:
                if cancel_first:
                    assert requests[0].cancel()
                owner.close()
                owner.close()
                assert all(not request.cancel() for request in requests)
                snapshot = owner.snapshot()
                assert snapshot.closed
                assert all(
                    entry.pending == 0 and entry.active == 1
                    for entry in snapshot.classes
                )
                assert [
                    entry.outcomes.rejected_closed for entry in snapshot.classes
                ] == [int(not cancel_first), 1]

            for index, result in enumerate(results):
                if cancel_first and index == 0:
                    with pytest.raises(AdmissionCancelled):
                        result.result(3)
                else:
                    with pytest.raises(AdmissionRejected) as raised:
                        result.result(3)
                    assert raised.value.reason is RejectionReason.CLOSED
        finally:
            for stream in streams:
                stream.close()
    counts = owner.snapshot().classes[0].outcomes
    assert (counts.cancelled, counts.rejected_closed, counts.failed) == (
        int(cancel_first),
        int(not cancel_first),
        0,
    )
    assert owner.wait_drained(timeout=1)


@pytest.mark.parametrize("_repetition", range(10))
def test_close_races_grant_without_interrupting_the_winner(_repetition: int) -> None:
    pressure = PressureClass("read")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    held, queued, release = Event(), Event(), Event()
    race = Barrier(2)
    calls: list[int] = []

    def active() -> None:
        held.set()
        assert release.wait(3)
        race.wait(3)

    def close() -> None:
        race.wait(3)
        owner.close()

    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    with ThreadPoolExecutor(3) as executor:
        first = executor.submit(owner.request(pressure).call, active)
        try:
            assert held.wait(3)
            with patch.object(owner._condition, "wait", side_effect=signal_wait):
                second = executor.submit(
                    owner.request(pressure).call, lambda: calls.append(1)
                )
                assert queued.wait(3)
                closer = executor.submit(close)
                release.set()
                closer.result(3)
                try:
                    second.result(3)
                except AdmissionRejected as error:
                    assert error.reason is RejectionReason.CLOSED
                    assert calls == []
                else:
                    assert calls == [1]
        finally:
            release.set()
        first.result(3)
    snapshot = owner.snapshot().classes[0]
    assert snapshot.active == snapshot.pending == 0
    assert snapshot.outcomes.completed + snapshot.outcomes.rejected_closed == 2
    assert snapshot.outcomes.failed == 0
