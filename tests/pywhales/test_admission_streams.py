from collections.abc import Iterator
from contextlib import AbstractContextManager
from types import TracebackType
from typing import override

import pytest

from pywhales.admission import (
    AdmissionOwner,
    AdmissionReentryError,
    AdmissionRejected,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.telemetry import AdmissionDecisionRecord, TelemetryRecord


class Source(AbstractContextManager[Iterator[int]], Iterator[int]):
    def __init__(self) -> None:
        self.values = iter([1, 2])
        self.iteration_error: BaseException | None = None
        self.entry_error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self.exits: list[BaseException | None] = []

    @override
    def __enter__(self) -> Iterator[int]:
        if self.entry_error is not None:
            raise self.entry_error
        return self

    @override
    def __iter__(self) -> Iterator[int]:
        return self

    @override
    def __next__(self) -> int:
        if self.iteration_error is not None:
            raise self.iteration_error
        return next(self.values)

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exits.append(exc_value)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return True


def _owner() -> tuple[AdmissionOwner, PressureClass]:
    pressure = PressureClass("stream")
    return AdmissionOwner(
        EndpointIdentity("local"), {pressure: ClassLimit(1, 0, demand_capacity=5)}
    ), pressure


def test_exhaustion_exits_cleanly_and_releases_once() -> None:
    owner, pressure = _owner()
    source = Source()
    stream = owner.request(pressure).stream(lambda: source)
    assert owner.snapshot().classes[0].active == 1
    assert list(stream) == [1, 2]
    stream.close()
    assert source.exits == [None]
    counts = owner.snapshot().classes[0]
    assert (
        counts.active,
        counts.outcomes.completed,
        counts.outcomes.stream_closed,
    ) == (0, 1, 0)


@pytest.mark.parametrize("consume", [False, True])
def test_close_before_read_or_after_bare_break(consume: bool) -> None:
    owner, pressure = _owner()
    source = Source()
    stream = owner.request(pressure).stream(lambda: source)
    if consume:
        for value in stream:
            assert value == 1
            break
    with pytest.raises(AdmissionRejected):
        owner.request(pressure).call(lambda: 1)
    stream.close()
    stream.close()
    assert source.exits == [None]
    assert owner.request(pressure).call(lambda: 3) == 3
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1


@pytest.mark.parametrize("stage", ["factory", "entry"])
def test_setup_failure_releases_without_exiting_partial_source(stage: str) -> None:
    pressure = PressureClass("stream")
    records: list[TelemetryRecord] = []
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0, demand_capacity=5)},
        telemetry_observer=records.append,
    )
    source = Source()
    error = KeyboardInterrupt()

    def factory() -> Source:
        if stage == "factory":
            raise error
        source.entry_error = error
        return source

    with pytest.raises(KeyboardInterrupt) as raised:
        owner.request(pressure, demand=4).stream(factory)
    assert raised.value is error
    assert source.exits == []
    assert owner.snapshot().classes[0].outcomes.failed == 1
    assert owner.snapshot().classes[0].demand.active == 0
    assert len(records) == 1
    assert isinstance(records[0], AdmissionDecisionRecord)
    assert records[0].outcome == "granted"
    assert owner.request(pressure).call(lambda: 4) == 4


@pytest.mark.parametrize("body", [False, True])
@pytest.mark.parametrize("cleanup", [False, True])
def test_original_failure_survives_suppression_and_cleanup_error(
    body: bool, cleanup: bool
) -> None:
    owner, pressure = _owner()
    source = Source()
    original, secondary = KeyboardInterrupt(), ValueError("cleanup")
    if not body:
        source.iteration_error = original
    if cleanup:
        source.cleanup_error = secondary
    stream = owner.request(pressure, demand=4).stream(lambda: source)
    assert stream.try_resize(3)
    with (
        pytest.raises(BaseExceptionGroup if cleanup else KeyboardInterrupt) as raised,
        stream,
    ):
        if body:
            raise original
        next(stream)
    if cleanup:
        assert isinstance(raised.value, BaseExceptionGroup)
        assert raised.value.exceptions == (original, secondary)
    else:
        assert raised.value is original
    assert source.exits == [original]
    assert owner.snapshot().classes[0].outcomes.failed == 1
    assert owner.snapshot().classes[0].active == 0
    assert owner.snapshot().classes[0].demand.active == 0


def test_clean_close_propagates_terminal_error_from_source_exit() -> None:
    owner, pressure = _owner()
    source = Source()
    error = ValueError("terminal")
    source.cleanup_error = error
    stream = owner.request(pressure).stream(lambda: source)
    with pytest.raises(ValueError) as raised:
        stream.close()
    assert raised.value is error
    stream.close()
    assert source.exits == [None]
    assert owner.snapshot().classes[0].outcomes.failed == 1


@pytest.mark.parametrize("stage", ["factory", "entry", "iteration", "cleanup"])
@pytest.mark.parametrize("drain", [False, True])
def test_stream_callbacks_guard_same_owner_reentry(stage: str, drain: bool) -> None:
    owner, pressure = _owner()

    def reenter() -> None:
        if drain:
            owner.close()
            owner.wait_drained(timeout=1)
        else:
            owner.request(pressure).call(lambda: 0)

    class ReentrantSource(Source):
        @override
        def __enter__(self) -> Iterator[int]:
            if stage == "entry":
                reenter()
            return super().__enter__()

        @override
        def __next__(self) -> int:
            if stage == "iteration":
                reenter()
            return super().__next__()

        @override
        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            if stage == "cleanup":
                reenter()
            return super().__exit__(exc_type, exc_value, traceback)

    def factory() -> ReentrantSource:
        if stage == "factory":
            reenter()
        return ReentrantSource()

    with pytest.raises(AdmissionReentryError):
        stream = owner.request(pressure).stream(factory)
        if stage == "iteration":
            next(stream)
        else:
            stream.close()
    assert owner.snapshot().classes[0].active == 0
