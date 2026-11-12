import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.components.image.streams import MultiImageStream
from pywhales.exceptions import DockerException
from pywhales.utils import MappedProcessStream, ProcessStream, stream_stdout_and_stderr


def _assert_reaped(source: ProcessStream) -> None:
    assert source._process.poll() is not None
    assert all(not thread.is_alive() for thread in source._threads)
    assert not source._watchdog.is_alive()
    assert all(
        pipe.closed
        for pipe in (source.stdin, source._process.stdout, source._process.stderr)
        if pipe is not None
    )


@pytest.mark.parametrize("mode", ["exhaust", "close", "body", "failure", "terminal"])
def test_process_stream_context_lifecycle_and_next_admission(mode: str) -> None:
    pressure = PressureClass("stream")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    children: list[ProcessStream] = []

    def factory() -> ProcessStream:
        code = "print('ready', flush=True)"
        if mode in ("close", "body"):
            code += "; input()"
        if mode in ("failure", "terminal"):
            code += "; raise SystemExit(7)"
        child = stream_stdout_and_stderr(
            [sys.executable, "-u", "-c", code], pipe_stdin=True, timeout=3
        )
        children.append(child)
        if mode == "terminal":
            child._on_failure(lambda error: None)
        return child

    stream = owner.request(pressure).stream(factory)
    try:
        if mode == "failure":
            with pytest.raises(DockerException):
                list(stream)
        elif mode == "terminal":
            assert children[0]._released.wait(3)
            with pytest.raises(DockerException):
                stream.close()
        elif mode == "body":
            original = ValueError("body")
            with pytest.raises(ValueError) as raised, stream:
                raise original
            assert raised.value is original
        elif mode == "close":
            assert next(stream) == ("stdout", b"ready\n")
            stream.close()
        else:
            assert list(stream) == [("stdout", b"ready\n")]
    finally:
        stream.close()
    _assert_reaped(children[0])
    assert owner.snapshot().classes[0].active == 0
    assert owner.request(pressure).call(lambda: 17) == 17


def test_process_source_exit_wakes_blocked_next_before_permit_release() -> None:
    pressure = PressureClass("stream")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    child = stream_stdout_and_stderr(
        [sys.executable, "-u", "-c", "print('ready', flush=True); input()"],
        pipe_stdin=True,
        timeout=3,
    )
    stream = owner.request(pressure).stream(lambda: child)
    reading = Event()
    get = child._core._queue.get

    def signal_read() -> tuple[str | None, bytes] | None:
        reading.set()
        return get()

    try:
        assert next(stream) == ("stdout", b"ready\n")
        with (
            patch.object(child._core._queue, "get", side_effect=signal_read),
            ThreadPoolExecutor(1) as executor,
        ):
            result = executor.submit(next, stream)
            assert reading.wait(3)
            stream.close()
            with pytest.raises(StopIteration):
                result.result(3)
    finally:
        stream.close()
    _assert_reaped(child)
    assert owner.snapshot().classes[0].active == 0


@pytest.mark.parametrize("failure", [False, True])
def test_one_permit_covers_composite_image_children_without_nested_admission(
    failure: bool,
) -> None:
    pressure = PressureClass("transfer")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    children: list[ProcessStream] = []

    def factory(name: str) -> MappedProcessStream[tuple[str, bytes]]:
        assert owner.snapshot().classes[0].active == 1
        code = f"print({name!r}, flush=True)"
        if failure and name == "bad":
            code += "; raise SystemExit(9)"
        child = stream_stdout_and_stderr(
            [sys.executable, "-u", "-c", code], pipe_stdin=True, timeout=3
        )
        children.append(child)
        return MappedProcessStream(child, ((name, data) for _, data in child))

    group = MultiImageStream(["first", "bad", "last"], factory)
    stream = owner.request(pressure).stream(lambda: group)
    try:
        assert children == []
        if failure:
            with pytest.raises(DockerException):
                list(stream)
        else:
            assert sorted(stream) == [
                ("bad", b"bad\n"),
                ("first", b"first\n"),
                ("last", b"last\n"),
            ]
    finally:
        stream.close()
    assert children
    for child in children:
        _assert_reaped(child)
    assert group._cleanup_done.is_set()
    assert owner.snapshot().classes[0].active_high_water == 1
    assert owner.request(pressure).call(lambda: 23) == 23


def test_launch_timeout_clock_is_unchanged_after_queue_wait() -> None:
    pressure = PressureClass("stream")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 1)})
    active, release, queued = Event(), Event(), Event()
    children: list[ProcessStream] = []
    launch_times: list[float] = []

    def hold() -> None:
        active.set()
        assert release.wait(3)

    wait = owner._condition.wait

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    def factory() -> ProcessStream:
        launch_times.append(monotonic())
        with patch("pywhales.utils.monotonic", return_value=launch_times[-1]):
            child = stream_stdout_and_stderr(
                [sys.executable, "-u", "-c", "print('launched', flush=True); input()"],
                pipe_stdin=True,
                timeout=30,
            )
        children.append(child)
        return child

    with (
        ThreadPoolExecutor(2) as executor,
        patch("pywhales.admission._owner.monotonic", side_effect=[100, 100, 1000]),
    ):
        first = executor.submit(owner.request(pressure).call, hold)
        try:
            assert active.wait(3)
            with patch.object(owner._condition, "wait", side_effect=signal_wait):
                second = executor.submit(
                    owner.request(pressure, queue_timeout=2000).stream, factory
                )
                assert queued.wait(3)
                release.set()
                stream = second.result(3)
            try:
                assert next(stream) == ("stdout", b"launched\n")
                assert children[0]._deadline == launch_times[0] + 30
                assert owner.snapshot().classes[0].active == 1
            finally:
                stream.close()
        finally:
            release.set()
        first.result(3)
    _assert_reaped(children[0])


@pytest.mark.parametrize("child_exited", [False, True])
def test_idle_real_child_stream_holds_drain_until_explicit_cleanup(
    child_exited: bool,
) -> None:
    pressure = PressureClass("stream")
    owner = AdmissionOwner(EndpointIdentity("local"), {pressure: ClassLimit(1, 0)})
    code = "print('ready', flush=True)"
    if not child_exited:
        code += "; input()"
    child = stream_stdout_and_stderr(
        [sys.executable, "-u", "-c", code], pipe_stdin=True
    )
    stream = owner.request(pressure).stream(lambda: child)
    try:
        if child_exited:
            child._process.wait(3)

        owner.close()

        assert not owner.wait_drained(timeout=0.001)
        assert owner.snapshot().classes[0].active == 1
        if not child_exited:
            assert child._process.poll() is None
            assert (
                child._process.stdout is not None and not child._process.stdout.closed
            )
        stream.close()
        assert owner.wait_drained(timeout=1)
    finally:
        stream.close()
    _assert_reaped(child)
    assert owner.snapshot().classes[0].outcomes.stream_closed == 1
