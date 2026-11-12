"""Admitted streams that callers drop without closing release their permit.

Sources are fakes or local Python children; no test sleeps or uses Docker.
"""

import gc
import signal
import sys
import weakref
from collections.abc import Iterator
from contextlib import AbstractContextManager
from threading import Event
from types import TracebackType
from typing import Literal, override

import pytest

from pywhales._process_stream import _ProcessCore
from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.client_config import ClientConfig
from pywhales.telemetry import CompletionRecord, TelemetryRecord
from pywhales.utils import ProcessStream, stream_stdout_and_stderr

BOUNDED_WAIT = 5.0


class GatedSource(AbstractContextManager[Iterator[int]], Iterator[int]):
    """Source whose exit signals entry, then waits until the test allows it."""

    def __init__(self) -> None:
        self.exiting = Event()
        self.allow_exit = Event()
        self.exits: list[BaseException | None] = []

    @override
    def __iter__(self) -> Iterator[int]:
        return self

    @override
    def __next__(self) -> int:
        return 1

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exiting.set()
        assert self.allow_exit.wait(BOUNDED_WAIT)
        self.exits.append(exc_value)


def owner_and_class() -> tuple[AdmissionOwner, PressureClass]:
    pressure = PressureClass("stream")
    return AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)}), (
        pressure
    )


def active(owner: AdmissionOwner) -> int:
    return owner.snapshot().classes[0].active


def test_dropped_stream_exits_its_source_before_releasing_the_permit() -> None:
    owner, pressure = owner_and_class()
    source = GatedSource()
    stream = owner.request(pressure).stream(lambda: source)
    assert next(stream) == 1
    handle = weakref.ref(stream)

    del stream
    gc.collect()

    assert handle() is None
    assert source.exiting.wait(BOUNDED_WAIT)
    assert active(owner) == 1
    source.allow_exit.set()
    assert owner.wait_drained(timeout=BOUNDED_WAIT)
    assert source.exits == [None]
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1


@pytest.mark.parametrize("lock", ["owner", "stream"])
def test_collection_while_holding_a_lock_defers_all_cleanup(
    lock: Literal["owner", "stream"],
) -> None:
    owner, pressure = owner_and_class()
    source = GatedSource()
    source.allow_exit.set()
    stream = owner.request(pressure).stream(lambda: source)
    held = owner._condition if lock == "owner" else stream._core._condition
    handle = weakref.ref(stream)
    gc.disable()
    try:
        # Only this explicit collection may free the cycle.
        cycle: list[object] = [stream]
        cycle.append(cycle)
        del stream, cycle
        with held:
            gc.collect()
            assert handle() is None
            # Both locks are reentrant: cleanup run by the finalizer itself
            # would have exited the source or released the permit here.
            assert not source.exiting.is_set()
            assert active(owner) == 1
    finally:
        gc.enable()

    assert owner.wait_drained(timeout=BOUNDED_WAIT)
    assert source.exits == [None]
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1


def test_dropped_process_source_is_reaped_with_one_closed_completion() -> None:
    owner, pressure = owner_and_class()
    records: list[TelemetryRecord] = []
    completed = Event()

    def observe(record: TelemetryRecord) -> None:
        records.append(record)
        if isinstance(record, CompletionRecord):
            completed.set()

    config = ClientConfig(client_call=[sys.executable], telemetry_observer=observe)
    cores: list[_ProcessCore] = []

    def factory() -> ProcessStream:
        child = stream_stdout_and_stderr(
            [*config.docker_cmd, "-c", "print('ready', flush=True); input()"],
            pipe_stdin=True,
        )
        cores.append(child._core)
        return child

    stream = owner.request(pressure).stream(factory)
    assert next(stream) == ("stdout", b"ready\n")

    del stream
    gc.collect()

    assert owner.wait_drained(timeout=BOUNDED_WAIT)
    assert completed.wait(BOUNDED_WAIT)
    (core,) = cores
    assert core._released.wait(BOUNDED_WAIT)
    core._watchdog.join(BOUNDED_WAIT)
    assert not core._watchdog.is_alive()
    assert core._process.returncode == -signal.SIGKILL
    completions = [r for r in records if isinstance(r, CompletionRecord)]
    assert [c.outcome for c in completions] == ["closed"]
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1
