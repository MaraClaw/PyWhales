"""Reclamation of process streams whose callers drop them without closing.

Real local Python children stand in for the Docker CLI. Tests drop the public
handle, collect it, and wait on events or bounded joins; nothing sleeps.
"""

import gc
import logging
import signal
import sys
import threading
import weakref
from collections.abc import Iterator
from threading import Event
from typing import Literal

import pytest

from pywhales._observation import observation
from pywhales._process_stream import _ProcessCore
from pywhales._reclaim import watch
from pywhales.client_config import ClientConfig
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import CompletionRecord, TelemetryRecord
from pywhales.utils import MappedProcessStream, ProcessStream, stream_stdout_and_stderr

BOUNDED_WAIT = 5.0
WAITING_CHILD = "print('ready', flush=True); input()"


class Records:
    """Telemetry observer that signals each completion record."""

    def __init__(self) -> None:
        self.records: list[TelemetryRecord] = []
        self.completed = Event()

    def __call__(self, record: TelemetryRecord) -> None:
        self.records.append(record)
        if isinstance(record, CompletionRecord):
            self.completed.set()

    def completions(self) -> list[CompletionRecord]:
        return [r for r in self.records if isinstance(r, CompletionRecord)]


class Probe:
    """Core stand-in whose reclamation sets an event."""

    def __init__(self) -> None:
        self.closed = Event()

    def close(self) -> None:
        self.closed.set()


class Handle:
    """Plain public object for watching a probe."""


def settle_reclaimer() -> None:
    """Wait until the reclaimer has handled everything queued before this call.

    The reclaimer is FIFO, so a probe queued now closes after earlier cores. A
    second probe also covers cores those reclamations freed and queued.
    """
    for _ in range(2):
        probe, handle = Probe(), Handle()
        watch(handle, probe)
        del handle
        assert probe.closed.wait(BOUNDED_WAIT)


def launch(observer: Records, code: str = WAITING_CHILD) -> ProcessStream:
    config = ClientConfig(client_call=[sys.executable], telemetry_observer=observer)
    return stream_stdout_and_stderr([*config.docker_cmd, "-c", code], pipe_stdin=True)


def assert_released(core: _ProcessCore) -> None:
    """Check the child was killed and reaped and every owned thread exited."""
    assert core._released.wait(BOUNDED_WAIT)
    for thread in (*core._threads, core._reaper, core._watchdog):
        thread.join(BOUNDED_WAIT)
        assert not thread.is_alive()
    assert core._process.returncode == -signal.SIGKILL
    assert all(
        pipe.closed
        for pipe in (core.stdin, core._process.stdout, core._process.stderr)
        if pipe is not None
    )


def child_policy() -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_concurrent_children=1,
    )


@pytest.mark.parametrize("kind", ["process", "mapped"])
def test_dropped_stream_is_closed_through_the_explicit_path(
    kind: Literal["process", "mapped"],
) -> None:
    observer = Records()
    with observation(child_policy()) as state:
        raw = launch(observer)
    core = raw._core
    stream: Iterator[object] = (
        raw if kind == "process" else MappedProcessStream(raw, (b for _, b in raw))
    )
    del raw
    assert next(stream) in {("stdout", b"ready\n"), b"ready\n"}
    assert state.child_allowance() == 0
    assert any(
        t.name == "pywhales-reclaimer" and t.daemon for t in threading.enumerate()
    )

    handle = weakref.ref(stream)
    del stream
    gc.collect()

    assert handle() is None
    assert observer.completed.wait(BOUNDED_WAIT)
    assert_released(core)
    assert state.child_allowance() == 1
    settle_reclaimer()
    completions = observer.completions()
    assert [(c.outcome, c.stop_reason) for c in completions] == [("closed", None)]
    assert completions[0].return_code == -signal.SIGKILL


def test_collection_while_holding_the_queue_lock_only_enqueues() -> None:
    observer = Records()
    stream = launch(observer)
    core = stream._core
    assert next(stream) == ("stdout", b"ready\n")
    handle = weakref.ref(stream)
    gc.disable()
    try:
        # Only this explicit collection may free the cycle.
        cycle: list[object] = [stream]
        cycle.append(cycle)
        del stream, cycle
        with core._queue.condition:
            gc.collect()
            assert handle() is None
            # The reclaimer needs this lock to stop the stream; the finalizer
            # ran on this thread inside the critical section and did nothing else.
            assert not core._stop.is_set()
            assert core._process.poll() is None
    finally:
        gc.enable()

    assert observer.completed.wait(BOUNDED_WAIT)
    assert_released(core)
    assert [c.outcome for c in observer.completions()] == ["closed"]


@pytest.mark.parametrize("ending", ["exhausted", "closed", "context"])
def test_finished_stream_keeps_its_single_completion_after_collection(
    ending: Literal["exhausted", "closed", "context"],
) -> None:
    observer = Records()
    stream = launch(
        observer, "print('done')" if ending == "exhausted" else WAITING_CHILD
    )
    match ending:
        case "exhausted":
            assert list(stream) == [("stdout", b"done\n")]
        case "closed":
            stream.close()
        case "context":
            with stream:
                pass
    assert observer.completed.wait(BOUNDED_WAIT)

    del stream
    gc.collect()
    settle_reclaimer()

    expected = "success" if ending == "exhausted" else "closed"
    assert [c.outcome for c in observer.completions()] == [expected]


def test_reclaimer_logs_failures_by_type_and_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing:
        def close(self) -> None:
            raise RuntimeError("confidential-payload")

    handle = Handle()
    watch(handle, Failing())
    with caplog.at_level(logging.WARNING, logger="pywhales._reclaim"):
        del handle
        gc.collect()
        settle_reclaimer()

    messages = [r.getMessage() for r in caplog.records if r.name == "pywhales._reclaim"]
    assert messages == [
        "Reclaiming an abandoned stream raised RuntimeError; details withheld"
    ]
    assert all(r.exc_info is None for r in caplog.records)
