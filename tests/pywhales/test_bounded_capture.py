import errno
import os
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Buffer, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Literal, TypedDict, assert_never, override
from unittest.mock import patch

import pytest

import pywhales._readiness
from pywhales import DockerClient
from pywhales._archive_deadline import ArchiveDeadline
from pywhales._capture_pipes import CapturePipes
from pywhales._observation import ObservationState, _ChildReservation, observation
from pywhales._process_stream import ProcessStream
from pywhales.client_config import ClientConfig
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    NoSuchContainer,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy, InventoryRequest, capture_inventory
from pywhales.utils import run, stream_stdout_and_stderr


def policy(response: int = 1024, argv: int = 65536) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=argv, max_response_bytes=response
    )


class OutputOptions(TypedDict, total=False):
    tty: bool
    capture_stdout: bool
    capture_stderr: bool


class RunOptions(OutputOptions, total=False):
    input: bytes | Iterable[bytes]


def test_combined_streams_at_exact_limit_and_decode() -> None:
    with observation(policy(8)):
        result = run(
            [
                sys.executable,
                "-c",
                "import os; os.write(1,b'ab\\xff\\n'); os.write(2,b'err!')",
            ],
            return_stderr=True,
        )
    assert result == ("ab\ufffd", "err!")


@pytest.mark.parametrize("descriptor", [1, 2])
def test_unterminated_output_over_limit_is_rejected(descriptor: int) -> None:
    with observation(policy(8)), pytest.raises(ObservationLimitError) as failure:
        run([sys.executable, "-c", f"import os; os.write({descriptor}, b'x'*9)"])
    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "response",
        8,
        9,
    )


def test_stdout_and_stderr_share_the_budget() -> None:
    with observation(policy(8)), pytest.raises(ObservationLimitError):
        run(
            [
                sys.executable,
                "-c",
                "import os; os.write(1,b'12345'); os.write(2,b'6789')",
            ]
        )


def test_effective_environment_is_checked_before_launch() -> None:
    with (
        observation(policy()),
        pytest.raises(ObservationLimitError) as failure,
    ):
        run([sys.executable, "-c", "pass"], env={"PRIVATE": "secret\0value"})
    assert failure.value.reason == "environment"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("layer", ["client", "operation"])
def test_invalid_environment_during_observation_is_safe_and_never_launches(
    layer: str,
) -> None:
    config = ClientConfig(client_call=[sys.executable])
    command = [*config.docker_cmd, "-c", "pass"]
    invalid = {"PRIVATE": "secret\0value"}
    if layer == "client":
        config.environment = invalid
    with (
        patch("pywhales._bounded_capture.subprocess.Popen") as launch,
        observation(policy()),
        pytest.raises(ObservationLimitError) as failure,
    ):
        run(command, env=invalid if layer == "operation" else {})
    assert failure.value.reason == "environment"
    assert "secret" not in "".join(traceback.format_exception(failure.value))
    launch.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        {"tty": True},
        {"capture_stdout": False},
        {"capture_stderr": False},
        {"input": b""},
        {"input": ()},
    ],
)
def test_unsupported_modes_fail_before_launch(options: RunOptions) -> None:
    with observation(policy()), pytest.raises(ValueError):
        run(["does-not-exist"], **options)


def test_pre_cancelled_scope_never_launches(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    cancel = Event()
    with pytest.raises(ObservationCancelled), observation(policy(), cancel=cancel):
        cancel.set()
        run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
                marker,
            ]
        )
    assert not marker.exists()


def test_expired_scope_never_launches(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    with pytest.raises(ObservationTimeout), observation(policy(), deadline=0):
        run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
                marker,
            ]
        )
    assert not marker.exists()


def test_cancellation_interrupts_read_and_reaps_child() -> None:
    cancel = Event()
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def launch(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    timer = Thread(target=lambda: (time.sleep(0.3), cancel.set()))
    timer.start()
    try:
        with (
            patch("pywhales._bounded_capture.subprocess.Popen", side_effect=launch),
            pytest.raises(ObservationCancelled),
            observation(policy(), cancel=cancel),
        ):
            run([sys.executable, "-c", "import threading; threading.Event().wait()"])
    finally:
        timer.join()
    assert len(processes) == 1
    process = processes[0]
    assert process.returncode is not None
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


@pytest.mark.parametrize("selected_first", [True, False])
def test_earliest_deadline_wins(selected_first: bool) -> None:
    expected = DockerTimeout if selected_first else ObservationTimeout
    with (
        pytest.raises(expected),
        observation(
            policy(), deadline=time.monotonic() + (2 if selected_first else 0.2)
        ),
    ):
        run(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            timeout=0.2 if selected_first else 2,
        )


@pytest.mark.parametrize("raw", [False, True])
def test_selected_timeout_preserves_partial_output_and_diagnostics(raw: bool) -> None:
    secret = "partial-secret"
    config = ClientConfig(client_call=[sys.executable], raw_diagnostics=raw)
    with pytest.raises(DockerTimeout) as failure, observation(policy()):
        run(
            [
                *config.docker_cmd,
                "-c",
                f"import os,threading; os.write(1,{secret.encode()!r}); threading.Event().wait()",
            ],
            timeout=0.5,
        )
    assert failure.value.stdout == secret
    assert (secret in str(failure.value)) is raw


@pytest.mark.parametrize("raw", [False, True])
def test_failure_preserves_classification_and_diagnostics(raw: bool) -> None:
    secret = "No such container: secret-value"
    config = ClientConfig(client_call=[sys.executable], raw_diagnostics=raw)
    with observation(policy()), pytest.raises(NoSuchContainer) as failure:
        run(
            [
                *config.docker_cmd,
                "-c",
                f"import sys; sys.stderr.write({secret!r}); sys.exit(7)",
            ]
        )
    assert failure.value.stderr == secret
    assert (secret in str(failure.value)) is raw


def test_native_e2big_is_translated_without_cause() -> None:
    with (
        observation(policy()),
        patch(
            "pywhales._bounded_capture.subprocess.Popen",
            side_effect=OSError(errno.E2BIG, "secret"),
        ),
        pytest.raises(ObservationLimitError) as failure,
    ):
        run([sys.executable, "-c", "pass"])
    assert failure.value.reason == "native_argv"
    assert failure.value.__cause__ is None
    assert "secret" not in str(failure.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fork and inherited descriptors")
@pytest.mark.parametrize("timeout", [None, 0.1])
def test_scope_deadline_interrupts_descendant_pipe_after_direct_child_exit(
    timeout: float | None,
) -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    try:
        code = (
            "import os,sys\n"
            "release,status=map(int,sys.argv[1:])\n"
            "if os.fork()==0:\n"
            " os.write(status,b'R'); os.read(release,1); os._exit(0)\n"
            "os._exit(0)\n"
        )
        with (
            pytest.raises(ObservationTimeout),
            observation(policy(), deadline=time.monotonic() + 0.5),
        ):
            run(
                [sys.executable, "-c", code, release_read, status_write],
                pass_fds=(release_read, status_write),
                timeout=timeout,
            )
        assert os.read(status_read, 1) == b"R"
    finally:
        os.close(release_read)
        os.close(status_write)
        os.close(release_write)
        assert os.read(status_read, 1) == b""
        os.close(status_read)


@dataclass(frozen=True)
class BackgroundCapture:
    """A bounded capture running in its own thread and its eventual outcome."""

    thread: Thread
    outcome: list[str | BaseException]


def capture_in_thread(
    args: Sequence[object],
    *,
    pass_fds: Sequence[int] = (),
    timeout: float | None = None,
    cancel: Event | None = None,
) -> BackgroundCapture:
    outcome: list[str | BaseException] = []

    def capture() -> None:
        try:
            with observation(policy(), cancel=cancel):
                outcome.append(run(args, pass_fds=pass_fds, timeout=timeout))
        except BaseException as error:
            outcome.append(error)

    thread = Thread(target=capture, daemon=True)
    thread.start()
    return BackgroundCapture(thread, outcome)


@pytest.fixture
def ready_timeouts(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    """Record the timeout of every capture wait."""
    timeouts: list[float | None] = []
    ready = CapturePipes.ready

    def record(self: CapturePipes, timeout: float | None) -> list[tuple[int, int]]:
        timeouts.append(timeout)
        return ready(self, timeout)

    monkeypatch.setattr(CapturePipes, "ready", record)
    return timeouts


_CAPTURE_HOLDER = (
    "import os,sys\n"
    "release,status=map(int,sys.argv[1:3])\n"
    "if os.fork()==0:\n"
    " os.write(status,b'R'); os.read(release,1); os._exit(0)\n"
)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors")
@pytest.mark.parametrize("scenario", ["eof_before_exit", "exit_before_eof"])
def test_capture_notices_exit_independently_of_pipe_eof_without_polling(
    scenario: str, ready_timeouts: list[float | None]
) -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    eof_first = scenario == "eof_before_exit"
    code = (
        # The child closes both outputs, then lives until released.
        "import os,sys\nrelease,status=map(int,sys.argv[1:3])\n"
        "os.close(1); os.close(2); os.write(status,b'R'); os.read(release,1)\n"
        if eof_first
        # The child exits at once while its descendant holds both outputs.
        else _CAPTURE_HOLDER + "os.write(1,b'done')\nos._exit(0)\n"
    )
    before = set(threading.enumerate())
    try:
        capture = capture_in_thread(
            [sys.executable, "-c", code, release_read, status_write],
            pass_fds=(release_read, status_write),
            timeout=None if eof_first else 0.2,
        )
        assert select.select([status_read], [], [], 3)[0]
        assert os.read(status_read, 1) == b"R"

        # When the selected timeout passes and the state is otherwise idle.
        time.sleep(0.6)
        settled = len(ready_timeouts)
        time.sleep(0.3)

        # Then the capture still waits, untimed and without spinning.
        assert capture.thread.is_alive() and not capture.outcome
        assert len(ready_timeouts) == settled <= 5
        assert ready_timeouts[-1] is None

        # And finishes as soon as the child exits or the descendant lets go.
        os.write(release_write, b"x")
        capture.thread.join(3)
        assert not capture.thread.is_alive()
        assert capture.outcome == ["" if eof_first else "done"]
        assert set(threading.enumerate()) == before
    finally:
        os.write(release_write, b"x")
        for descriptor in (release_read, release_write, status_write):
            os.close(descriptor)
        assert os.read(status_read, 1) == b""
        os.close(status_read)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors")
def test_selected_timeout_waits_for_the_remaining_time_in_one_wait(
    ready_timeouts: list[float | None],
) -> None:
    before = set(threading.enumerate())

    with observation(policy()), pytest.raises(DockerTimeout):
        run(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            timeout=0.6,
        )

    assert 1 <= len(ready_timeouts) <= 2
    assert ready_timeouts[0] is not None and 0.3 < ready_timeouts[0] <= 0.6
    assert set(threading.enumerate()) == before


def test_cancel_token_keeps_bounded_waits(ready_timeouts: list[float | None]) -> None:
    cancel = Event()
    capture = capture_in_thread(
        [sys.executable, "-c", "import threading; threading.Event().wait()"],
        cancel=cancel,
    )
    time.sleep(0.3)

    cancel.set()
    capture.thread.join(3)

    assert not capture.thread.is_alive()
    assert isinstance(capture.outcome[0], ObservationCancelled)
    assert ready_timeouts
    assert all(timeout is not None and timeout <= 0.05 for timeout in ready_timeouts)


def test_capture_helper_start_failure_unwinds_child_and_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def launch(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    class FailingThread(Thread):
        @override
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(pywhales._readiness, "Thread", FailingThread)
    before = set(threading.enumerate())

    with (
        patch("pywhales._bounded_capture.subprocess.Popen", side_effect=launch),
        observation(policy()),
        pytest.raises(RuntimeError),
    ):
        run([sys.executable, "-c", "import threading; threading.Event().wait()"])

    (process,) = processes
    assert process.returncode == -signal.SIGKILL
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed
    assert set(threading.enumerate()) == before


COUNT_STDIN = "import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"
NEVER_READS_STDIN = "import threading; threading.Event().wait()"


def stdin_policy(limit: int, *, response: int = 1024) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=response,
        max_total_stdin_bytes=limit,
    )


def pieces(payload: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


@pytest.fixture
def children() -> Generator[list[subprocess.Popen[bytes]]]:
    """Record scoped captured, stream, and archive children, stdin pipe included."""
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def launch(
        args: list[str],
        *,
        stdout: int,
        stderr: int | None,
        env: Mapping[str, str],
        pass_fds: Sequence[int] = (),
        stdin: int | None = None,
        bufsize: int = -1,
    ) -> subprocess.Popen[bytes]:
        process = popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            pass_fds=pass_fds,
            bufsize=bufsize,
        )
        processes.append(process)
        return process

    with (
        patch("pywhales._bounded_capture.subprocess.Popen", side_effect=launch),
        patch("pywhales.utils.Popen", side_effect=launch),
        patch("pywhales.components.image.cli_wrapper.Popen", side_effect=launch),
    ):
        yield processes


@pytest.fixture
def owned_threads_end() -> Generator[None]:
    """Fail a test that leaves a writer, reaper or watcher thread behind."""
    before = set(threading.enumerate())
    yield
    assert set(threading.enumerate()) - before == set()


def assert_released(process: subprocess.Popen[bytes]) -> None:
    """Check the child was reaped and none of its pipes is left open."""
    assert process.returncode is not None
    assert all(
        pipe.closed
        for pipe in (process.stdin, process.stdout, process.stderr)
        if pipe is not None
    )


@pytest.mark.parametrize("form", ["bytes", "chunks"])
def test_upload_ending_exactly_at_the_budget_succeeds(form: str) -> None:
    payload = b"0123456789"

    with observation(stdin_policy(10)) as state:
        result = run(
            [sys.executable, "-c", COUNT_STDIN],
            input=payload if form == "bytes" else pieces(payload, 3),
        )

    assert result == "10"
    assert state.stdin_accounts[0].consumed == 10


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize("form", ["bytes", "chunks"])
def test_upload_past_the_budget_stops_at_the_limit_and_reaps_the_child(
    form: str, children: list[subprocess.Popen[bytes]]
) -> None:
    payload = b"0123456789A"

    with (
        observation(stdin_policy(10)) as state,
        pytest.raises(ObservationLimitError) as failure,
    ):
        run(
            [sys.executable, "-c", COUNT_STDIN],
            input=payload if form == "bytes" else pieces(payload, 4),
        )

    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "total_stdin",
        10,
        11,
    )
    assert state.stdin_accounts[0].consumed == 10
    (child,) = children
    assert_released(child)


@pytest.mark.parametrize("payload", [b"", (), (b"", b"")])
def test_empty_input_sends_eof_and_consumes_nothing(
    payload: bytes | Iterable[bytes],
) -> None:
    with observation(stdin_policy(1)) as state:
        assert run([sys.executable, "-c", COUNT_STDIN], input=payload) == "0"

    assert state.stdin_accounts[0].consumed == 0


def test_input_is_pulled_lazily_and_never_past_the_failing_chunk() -> None:
    pulled: list[int] = []

    def chunks() -> Generator[bytes]:
        for index in range(3):
            pulled.append(index)
            yield b"x" * 6

    with observation(stdin_policy(10)) as state, pytest.raises(ObservationLimitError):
        run([sys.executable, "-c", COUNT_STDIN], input=chunks())

    assert pulled == [0, 1]
    assert state.stdin_accounts[0].consumed == 10


def test_a_failing_producer_surfaces_unchanged_after_the_child_is_reaped(
    children: list[subprocess.Popen[bytes]],
) -> None:
    original = ValueError("private-producer-error")

    def chunks() -> Generator[bytes]:
        yield b"accepted"
        raise original

    with observation(stdin_policy(100)) as state, pytest.raises(ValueError) as raised:
        run([sys.executable, "-c", COUNT_STDIN], input=chunks())

    assert raised.value is original
    assert state.stdin_accounts[0].consumed == 8
    assert_released(children[0])


def test_output_is_drained_while_a_large_upload_is_pending() -> None:
    flood = 1024 * 1024
    upload = b"u" * (512 * 1024)
    code = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'o' * {flood}); sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write(b'e' * {flood}); sys.stderr.buffer.flush()\n"
        "sys.stdout.write(str(len(sys.stdin.buffer.read())))\n"
    )
    scope = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=4 * flood,
        max_total_stdin_bytes=len(upload),
    )

    # The child fills both output pipes before it reads any input, so a call that
    # finished the upload first would deadlock until the timeout.
    with observation(scope) as state:
        out, err = run(
            [sys.executable, "-c", code],
            input=pieces(upload, 65536),
            return_stderr=True,
            timeout=30,
        )

    assert out == "o" * flood + str(len(upload))
    assert err == "e" * flood
    assert state.stdin_accounts[0].consumed == len(upload)


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize("stop", ["cancel", "scope_deadline", "timeout"])
def test_a_blocked_upload_stops_on_its_first_stop_and_reaps_the_child(
    stop: str, children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked, cancel = Event(), Event()
    write = os.write

    def watch(fd: int, data: Buffer) -> int:
        try:
            return write(fd, data)
        except BlockingIOError:
            blocked.set()
            raise

    def cancel_once_blocked() -> None:
        if blocked.wait(timeout=10):
            cancel.set()

    monkeypatch.setattr(os, "write", watch)
    payload = b"x" * (4 * 1024 * 1024)
    expected = {
        "cancel": ObservationCancelled,
        "scope_deadline": ObservationTimeout,
        "timeout": DockerTimeout,
    }[stop]
    canceller = Thread(target=cancel_once_blocked) if stop == "cancel" else None
    if canceller is not None:
        canceller.start()
    scopes: list[ObservationState] = []
    try:
        with (
            pytest.raises(expected),
            observation(
                stdin_policy(2 * len(payload)),
                deadline=time.monotonic() + 0.5 if stop == "scope_deadline" else None,
                cancel=cancel if stop == "cancel" else None,
            ) as state,
        ):
            scopes.append(state)
            run(
                [sys.executable, "-c", NEVER_READS_STDIN],
                input=payload,
                timeout=0.5 if stop == "timeout" else None,
            )
    finally:
        if canceller is not None:
            canceller.join(timeout=10)

    assert 0 < scopes[0].stdin_accounts[0].consumed < len(payload)
    assert_released(children[0])


def test_child_exiting_early_keeps_only_the_accepted_input(
    children: list[subprocess.Popen[bytes]],
) -> None:
    payload = b"x" * (2 * 1024 * 1024)

    with observation(stdin_policy(len(payload))) as state:
        result = run(
            [sys.executable, "-c", "import os; os.read(0, 1); print('done')"],
            input=payload,
        )

    assert result == "done"
    assert 0 < state.stdin_accounts[0].consumed < len(payload)
    assert_released(children[0])


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize("layout", ["shared", "nested"])
def test_competing_uploads_charge_every_enclosing_account_and_never_pass_a_limit(
    layout: str,
) -> None:
    gate = Barrier(2)
    outcomes: list[str | BaseException] = []
    inner_totals: list[int] = []

    def upload() -> Iterator[bytes]:
        yield b"a" * 30
        # Both uploads have had their first chunk accepted before either goes on.
        gate.wait(timeout=10)
        yield b"b" * 40

    def compete() -> None:
        try:
            if layout == "nested":
                with observation(stdin_policy(80)) as inner:
                    try:
                        outcomes.append(
                            run([sys.executable, "-c", COUNT_STDIN], input=upload())
                        )
                    finally:
                        inner_totals.append(inner.stdin_accounts[1].consumed)
            else:
                outcomes.append(
                    run([sys.executable, "-c", COUNT_STDIN], input=upload())
                )
        except BaseException as error:
            outcomes.append(error)

    with observation(stdin_policy(100)) as state:
        workers = [Thread(target=copy_context().run, args=(compete,)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
            assert not worker.is_alive()

    # One upload fits whole, the other is cut at the shared limit: none is lost,
    # and no capacity is left reserved by the writer that finished first.
    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "ObservationLimitError",
        "str",
    ]
    assert "70" in outcomes
    assert state.stdin_accounts[0].consumed == 100
    if layout == "nested":
        assert sorted(inner_totals) == [30, 70]


def test_stdin_bytes_never_count_against_the_response_budget() -> None:
    scope = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_response_bytes=10,
        max_total_stdin_bytes=1000,
    )

    with observation(scope) as state:
        result = run(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); print('ok')"],
            input=b"x" * 500,
        )

    assert result == "ok"
    assert state.stdin_accounts[0].consumed == 500
    assert state.response_accounts[0].consumed == 3


@pytest.mark.parametrize(
    "options", [{"tty": True}, {"capture_stdout": False}, {"capture_stderr": False}]
)
def test_a_stdin_budget_keeps_the_tty_and_uncaptured_output_refusals(
    options: OutputOptions,
) -> None:
    with (
        patch("pywhales._bounded_capture.subprocess.Popen") as launch,
        observation(stdin_policy(10)),
        pytest.raises(ValueError),
    ):
        run(["does-not-exist"], input=b"x", **options)

    launch.assert_not_called()


def test_a_stopped_scope_launches_nothing_and_never_pulls_input() -> None:
    pulled: list[int] = []
    cancel = Event()

    def chunks() -> Generator[bytes]:
        pulled.append(1)
        yield b"x"

    with (
        patch("pywhales._bounded_capture.subprocess.Popen") as launch,
        pytest.raises(ObservationCancelled),
        observation(stdin_policy(10), cancel=cancel),
    ):
        cancel.set()
        run([sys.executable, "-c", COUNT_STDIN], input=chunks())

    launch.assert_not_called()
    assert pulled == []


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize("failure", ["spawn", "exit", "response"])
def test_launch_reservation_is_not_refunded_after_failure(
    failure: str, children: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    command = {
        "spawn": [str(tmp_path / "missing-cli")],
        "exit": [sys.executable, "-c", "raise SystemExit(7)"],
        "response": [sys.executable, "-c", "print('x' * 20)"],
    }[failure]
    expected = {
        "spawn": FileNotFoundError,
        "exit": DockerException,
        "response": ObservationLimitError,
    }[failure]
    with observation(
        replace(policy(8), max_total_launch_attempts=1, max_concurrent_children=1)
    ) as state:
        with pytest.raises(expected):
            run(command)
        with pytest.raises(ObservationLimitError) as refused:
            run([sys.executable, "-c", "pass"])
        assert refused.value.reason == "total_launch_attempts"
        assert state.launch_accounts[0].consumed == 1
        assert state.child_accounts[0].consumed == 0

    assert len(children) == (0 if failure == "spawn" else 1)
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_interrupted_child_is_reaped_without_refunding_its_launch(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = Event()
    ready = CapturePipes.ready

    def cancel_when_ready(
        self: CapturePipes, timeout: float | None
    ) -> list[tuple[int, int]]:
        result = ready(self, timeout)
        if result:
            cancel.set()
        return result

    monkeypatch.setattr(CapturePipes, "ready", cancel_when_ready)
    with observation(
        replace(policy(), max_total_launch_attempts=1), cancel=cancel
    ) as state:
        with pytest.raises(ObservationCancelled):
            run(
                [
                    sys.executable,
                    "-c",
                    "import threading; print('ready', flush=True); threading.Event().wait()",
                ]
            )
        cancel.clear()
        with pytest.raises(ObservationLimitError) as refused:
            run([sys.executable, "-c", "pass"])
        assert refused.value.reason == "total_launch_attempts"
        assert state.launch_accounts[0].consumed == 1

    assert len(children) == 1
    assert_released(children[0])


ARCHIVE_CLI = """
import json, sys
if sys.argv[1:3] == ['image', 'inspect']:
    print(json.dumps([{'Id': value} for value in sys.argv[3:]]))
elif sys.argv[1:3] == ['image', 'load']:
    sys.stdin.buffer.read()
    print('Loaded image: sample')
elif sys.argv[1:3] == ['image', 'save']:
    sys.stdout.buffer.write(b'archive')
else:
    print('stream')
"""


@pytest.mark.usefixtures("owned_threads_end")
def test_real_captured_stream_and_archive_children_share_one_launch_budget(
    children: list[subprocess.Popen[bytes]],
) -> None:
    client = DockerClient(client_call=[sys.executable, "-c", ARCHIVE_CLI])
    bounds = replace(
        policy(),
        max_total_launch_attempts=5,
        max_total_stdin_bytes=1024,
        max_concurrent_children=1,
    )
    with observation(bounds) as state:
        assert run(client.docker_cmd) == "stream"
        with stream_stdout_and_stderr(client.docker_cmd) as stream:
            assert list(stream) == [("stdout", b"stream\n")]
        saved = client.image.save("sha256:" + "a" * 64)
        assert state.launch_accounts[0].consumed == 3
        assert client.image.load(b"archive") == ["sample"]
        assert b"".join(saved) == b"archive"
        with pytest.raises(ObservationLimitError) as refused:
            run(client.docker_cmd)
        assert refused.value.reason == "total_launch_attempts"
        assert state.launch_accounts[0].consumed == 5
        assert state.child_accounts[0].consumed == 0
    assert len(children) == 5
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize("same_scope", [True, False])
def test_lazy_save_charges_eager_inspect_then_first_iteration_scope(
    children: list[subprocess.Popen[bytes]], same_scope: bool
) -> None:
    client = DockerClient(client_call=[sys.executable, "-c", ARCHIVE_CLI])
    bounds = replace(policy(), max_total_launch_attempts=1, max_concurrent_children=1)
    with observation(bounds) as creator:
        saved = client.image.save("sha256:" + "a" * 64)
        assert isinstance(saved, Generator)
        assert creator.launch_accounts[0].consumed == 1
        if same_scope:
            with pytest.raises(ObservationLimitError) as refused:
                next(saved)
            assert refused.value.reason == "total_launch_attempts"
    if not same_scope:
        with observation(bounds) as iterator:
            assert b"".join(saved) == b"archive"
            assert iterator.launch_accounts[0].consumed == 1
        assert creator.launch_accounts[0].consumed == 1
    assert len(children) == (1 if same_scope else 2)
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_copied_contexts_compete_for_shared_child_slots_and_reuse_after_close(
    children: list[subprocess.Popen[bytes]],
) -> None:
    start = Barrier(6)
    outcomes: list[ProcessStream | BaseException] = []
    bounds = replace(policy(), max_concurrent_children=2, max_total_launch_attempts=3)

    def launch() -> None:
        try:
            with observation(replace(bounds, max_concurrent_children=1)):
                start.wait(timeout=5)
                outcomes.append(
                    stream_stdout_and_stderr(
                        [
                            sys.executable,
                            "-c",
                            "import threading; threading.Event().wait()",
                        ]
                    )
                )
        except BaseException as error:
            outcomes.append(error)

    with observation(bounds) as state:
        workers = [Thread(target=copy_context().run, args=(launch,)) for _ in range(6)]
        streams: list[ProcessStream] = []
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)
                assert not worker.is_alive()
            streams = [value for value in outcomes if isinstance(value, ProcessStream)]
            failures = [value for value in outcomes if isinstance(value, BaseException)]
            assert len(streams) == 2
            assert len(failures) == 4
            assert all(
                isinstance(error, ObservationLimitError)
                and error.reason == "concurrent_children"
                for error in failures
            )
            assert state.child_accounts[0].consumed == 2
            assert state.launch_accounts[0].consumed == 2
            streams[0].close()
            streams[0].close()
            assert state.child_accounts[0].consumed == 1
            assert run([sys.executable, "-c", "print('reused')"]) == "reused"
            assert state.launch_accounts[0].consumed == 3
            assert state.child_accounts[0].consumed == 1
        finally:
            for stream in streams:
                stream.close()
        assert state.child_accounts[0].consumed == 0
    assert len(children) == 3
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_capture_returns_child_slot_while_pipe_drain_is_paused(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped, drain = Event(), Event()
    released = _ChildReservation.release
    ready = CapturePipes.ready
    outcomes: list[str | BaseException] = []

    def release(self: _ChildReservation) -> None:
        released(self)
        reaped.set()

    def paused(self: CapturePipes, timeout: float | None) -> list[tuple[int, int]]:
        if threading.current_thread() is worker:
            assert drain.wait(timeout=5)
        return ready(self, timeout)

    def capture() -> None:
        try:
            outcomes.append(run([sys.executable, "-c", "print('buffered')"]))
        except BaseException as error:
            outcomes.append(error)

    monkeypatch.setattr(_ChildReservation, "release", release)
    monkeypatch.setattr(CapturePipes, "ready", paused)
    with observation(replace(policy(), max_concurrent_children=1)) as state:
        worker = Thread(target=copy_context().run, args=(capture,))
        worker.start()
        try:
            assert reaped.wait(timeout=5)
            assert worker.is_alive()
            assert state.child_accounts[0].consumed == 0
            assert run([sys.executable, "-c", "print('reused')"]) == "reused"
        finally:
            drain.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert outcomes == ["buffered"]
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_stream_returns_child_slot_with_a_saturated_undrained_queue(
    children: list[subprocess.Popen[bytes]],
) -> None:
    with observation(replace(policy(), max_concurrent_children=1)) as state:
        with stream_stdout_and_stderr(
            [sys.executable, "-c", "import os; os.write(1, b'a\\nb\\nc\\n')"],
            buffer_bytes=2,
        ) as stream:
            stream._reaper.join(timeout=5)
            assert not stream._reaper.is_alive()
            assert state.child_accounts[0].consumed == 0
            assert not stream._released.is_set()
            assert run([sys.executable, "-c", "print('reused')"]) == "reused"
            assert list(stream) == [
                ("stdout", b"a\n"),
                ("stdout", b"b\n"),
                ("stdout", b"c\n"),
            ]
        assert state.child_accounts[0].consumed == 0
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_archive_load_releases_before_a_stalled_input_producer_finishes(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, resume, reaped = Event(), Event(), Event()
    released = _ChildReservation.release
    outcomes: list[list[str] | BaseException] = []

    def release(self: _ChildReservation) -> None:
        released(self)
        reaped.set()

    def chunks() -> Iterator[bytes]:
        entered.set()
        assert resume.wait(timeout=5)
        yield b""

    client = DockerClient(
        client_call=[sys.executable, "-c", "print('Loaded image: sample')"]
    )

    def load() -> None:
        try:
            outcomes.append(client.image.load(chunks()))
        except BaseException as error:
            outcomes.append(error)

    monkeypatch.setattr(_ChildReservation, "release", release)
    with observation(replace(policy(), max_concurrent_children=1)) as state:
        worker = Thread(target=copy_context().run, args=(load,))
        worker.start()
        try:
            assert entered.wait(timeout=5)
            assert reaped.wait(timeout=5)
            assert worker.is_alive()
            assert state.child_accounts[0].consumed == 0
            assert run([sys.executable, "-c", "print('reused')"]) == "reused"
        finally:
            resume.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert outcomes == [["sample"]]
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_lazy_save_releases_at_reap_before_resume_and_close_never_releases_twice(
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped = Event()
    owners: list[ArchiveDeadline] = []
    publish = ArchiveDeadline._publish_exit

    def publish_exit(self: ArchiveDeadline) -> None:
        publish(self)
        owners.append(self)
        reaped.set()

    monkeypatch.setattr(ArchiveDeadline, "_publish_exit", publish_exit)
    client = DockerClient(client_call=[sys.executable, "-c", ARCHIVE_CLI])
    bounds = replace(policy(), max_concurrent_children=1, max_total_launch_attempts=3)
    with observation(bounds) as creator:
        saved = client.image.save("sha256:" + "a" * 64)
        assert isinstance(saved, Generator)
        assert creator.launch_accounts[0].consumed == 1
        assert creator.child_accounts[0].consumed == 0
    with observation(bounds) as iterator:
        try:
            assert next(saved) == b"archive"
            assert reaped.wait(timeout=5)
            assert iterator.child_accounts[0].consumed == 0
            assert iterator.launch_accounts[0].consumed == 1
            assert creator.launch_accounts[0].consumed == 1
            assert owners[0]._scope is iterator
            assert owners[0]._reaper is None
            with stream_stdout_and_stderr(
                [sys.executable, "-c", "import threading; threading.Event().wait()"]
            ):
                saved.close()
                saved.close()
                assert iterator.child_accounts[0].consumed == 1
        finally:
            saved.close()
        assert iterator.child_accounts[0].consumed == 0
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
@pytest.mark.parametrize("ending", ["spawn", "setup", "exit", "cancel", "timeout"])
def test_terminal_child_paths_return_capacity_without_refunding_attempts(
    family: Literal["captured", "stream", "archive_load", "archive_save"],
    ending: str,
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Event()
    code = (
        "raise SystemExit(7)"
        if ending == "exit"
        else "import os, threading; os.write(1,b'ready\\n'); threading.Event().wait()"
    )
    client = DockerClient(client_call=[sys.executable, "-c", code])
    bounds = replace(
        policy(),
        max_concurrent_children=1,
        max_total_launch_attempts=2,
        max_total_stdin_bytes=1024,
    )
    timeout = 0.2 if ending == "timeout" else None
    ready = CapturePipes.ready

    def cancel_on_output(
        self: CapturePipes,
        remaining: float | None,
    ) -> list[tuple[int, int]]:
        result = ready(self, remaining)
        if result:
            cancel.set()
        return result

    def chunks() -> Iterator[bytes]:
        if ending == "cancel":
            cancel.set()
        yield b"archive"

    def execute() -> None:
        match family:
            case "captured":
                run(client.docker_cmd, timeout=timeout)
            case "stream":
                with stream_stdout_and_stderr(
                    client.docker_cmd, timeout=timeout
                ) as stream:
                    for _ in stream:
                        if ending == "cancel":
                            cancel.set()
            case "archive_load":
                client.image.load(chunks(), timeout=timeout)
            case "archive_save":
                saved = client.image.save("sample", timeout=timeout)
                assert isinstance(saved, Generator)
                try:
                    for _ in saved:
                        if ending == "cancel":
                            cancel.set()
                finally:
                    saved.close()
            case unreachable:
                assert_never(unreachable)

    expected = {
        "spawn": FileNotFoundError,
        "setup": RuntimeError,
        "exit": DockerException,
        "cancel": ObservationCancelled,
        "timeout": DockerTimeout,
    }[ending]
    with observation(bounds, cancel=cancel) as state:
        with ExitStack() as patches:
            patches.enter_context(
                patch.object(client.image, "inspect", return_value=[])
            )
            if ending == "spawn":
                target = {
                    "captured": "pywhales._bounded_capture.subprocess.Popen",
                    "stream": "pywhales.utils.Popen",
                    "archive_load": "pywhales.components.image.cli_wrapper.Popen",
                    "archive_save": "pywhales.components.image.cli_wrapper.Popen",
                }[family]
                patches.enter_context(patch(target, side_effect=FileNotFoundError))
            if ending == "setup":
                target = {
                    "captured": "pywhales._bounded_capture.watch_exit",
                    "stream": "pywhales.utils.ProcessStream",
                    "archive_load": "pywhales.components.image.cli_wrapper.ArchiveDeadline",
                    "archive_save": "pywhales.components.image.cli_wrapper.ArchiveDeadline",
                }[family]
                patches.enter_context(patch(target, side_effect=RuntimeError("setup")))
            if ending == "cancel" and family == "captured":
                monkeypatch.setattr(CapturePipes, "ready", cancel_on_output)
            try:
                with pytest.raises(expected):
                    execute()
            finally:
                cancel.clear()
                monkeypatch.setattr(CapturePipes, "ready", ready)
        assert state.child_accounts[0].consumed == 0
        assert state.launch_accounts[0].consumed == 1
        for child in children:
            assert_released(child)
        assert run([sys.executable, "-c", "print('reused')"]) == "reused"
        assert state.child_accounts[0].consumed == 0
        assert state.launch_accounts[0].consumed == 2
    assert len(children) == (1 if ending == "spawn" else 2)
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
@pytest.mark.parametrize(
    ("family", "fail_at"),
    [
        ("captured", 1),
        ("stream", 1),
        ("stream", 2),
        ("stream", 3),
        ("stream", 4),
        ("archive_load", 1),
        ("archive_load", 2),
        ("archive_save", 1),
        ("archive_save", 2),
        ("archive_load", 0),
        ("archive_save", 0),
    ],
)
def test_partial_worker_and_pipe_setup_failure_reaps_before_returning_capacity(
    family: str,
    fail_at: int,
    children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts = 0
    start = Thread.start

    def failing_start(self: Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == fail_at:
            raise RuntimeError("thread setup")
        start(self)

    def failing_blocking(fd: int, blocking: bool) -> None:
        raise RuntimeError("pipe setup")

    client = DockerClient(
        client_call=[sys.executable, "-c", "import threading; threading.Event().wait()"]
    )
    bounds = replace(policy(), max_concurrent_children=1, max_total_launch_attempts=2)
    with observation(bounds) as state:
        with monkeypatch.context() as setup:
            setup.setattr(Thread, "start", failing_start)
            if fail_at == 0:
                setup.setattr(os, "set_blocking", failing_blocking)
            with (
                patch.object(client.image, "inspect", return_value=[]),
                pytest.raises(RuntimeError),
            ):
                if family == "captured":
                    run(client.docker_cmd)
                elif family == "stream":
                    stream_stdout_and_stderr(client.docker_cmd)
                elif family == "archive_load":
                    client.image.load(b"archive", timeout=30)
                else:
                    next(client.image.save("sample", timeout=30))
        assert state.child_accounts[0].consumed == 0
        assert state.launch_accounts[0].consumed == 1
        assert len(children) == 1
        assert_released(children[0])
        assert run([sys.executable, "-c", "print('reused')"]) == "reused"
        assert state.child_accounts[0].consumed == 0
        assert state.launch_accounts[0].consumed == 2
    for child in children:
        assert_released(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptors")
@pytest.mark.usefixtures("owned_threads_end")
def test_output_eof_does_not_return_capacity_before_child_reap(
    children: list[subprocess.Popen[bytes]],
) -> None:
    gate_read, gate_write = os.pipe()
    ready_read, ready_write = os.pipe()
    try:
        bounds = replace(
            policy(), max_concurrent_children=1, max_total_launch_attempts=2
        )
        with (
            observation(bounds) as state,
            stream_stdout_and_stderr(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; os.close(1); os.close(2); "
                    "os.write(int(sys.argv[2]),b'R'); os.read(int(sys.argv[1]),1)",
                    gate_read,
                    ready_write,
                ],
                pass_fds=(gate_read, ready_write),
            ) as stream,
        ):
            assert select.select([ready_read], [], [], 5)[0]
            assert os.read(ready_read, 1) == b"R"
            for reader in stream._threads:
                reader.join(timeout=5)
                assert not reader.is_alive()
            assert state.child_accounts[0].consumed == 1
            with pytest.raises(ObservationLimitError) as refused:
                run([sys.executable, "-c", "pass"])
            assert refused.value.reason == "concurrent_children"
            assert state.launch_accounts[0].consumed == 1
            os.write(gate_write, b"X")
            stream._reaper.join(timeout=5)
            assert not stream._reaper.is_alive()
            assert state.child_accounts[0].consumed == 0
            assert run([sys.executable, "-c", "print('reused')"]) == "reused"
    finally:
        for fd in (gate_read, gate_write, ready_read, ready_write):
            os.close(fd)
    for child in children:
        assert_released(child)


@pytest.mark.usefixtures("owned_threads_end")
def test_inventory_plan_does_not_reserve_child_slots_for_sequential_chunks(
    children: list[subprocess.Popen[bytes]],
) -> None:
    client = DockerClient(client_call=[sys.executable, "-c", ARCHIVE_CLI])
    bounds = replace(policy(), max_concurrent_children=1, max_total_launch_attempts=3)
    references = tuple("sha256:" + value * 64 for value in "abc")
    with observation(bounds) as state:
        snapshot = capture_inventory(
            [
                InventoryRequest(
                    kind="image",
                    references=references,
                    client_config=client.client_config,
                )
            ],
            policy=bounds,
        )
        assert tuple(record.id for record in snapshot.records) == references
        assert state.launch_accounts[0].consumed == 3
        assert state.child_accounts[0].consumed == 0
        assert capture_inventory((), policy=bounds).records == ()
    assert len(children) == 3
    for child in children:
        assert_released(child)
