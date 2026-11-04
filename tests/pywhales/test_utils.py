"""Conversion and process-stream lifecycle regressions using real local children.

These tests launch Python and shell commands to exercise pipes, threads, buffering,
and deadlines. Docker wrapper paths are patched where used; daemon availability
does not replace the local process requirements.
"""

import errno
import os
import select
import signal
import subprocess
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Buffer, Callable, Mapping, Sequence
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Literal, assert_never, override

import pytest

import pywhales._process_stream
import pywhales._readiness
import pywhales._stdin_pump
import pywhales.utils
from pywhales._observation import ObservationState, _current_observation, observation
from pywhales._readiness import CANCEL_CHECK, WakeLatch, wait_ready
from pywhales._stdin_pump import StdinPump
from pywhales._stream_buffer import StreamBuffer
from pywhales.client_config import ClientConfig
from pywhales.components.buildx.cli_wrapper import stream_buildx_logs
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    NoSuchContainer,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry._lifecycle import ExecutionTelemetry
from pywhales.utils import (
    MappedProcessStream,
    ProcessStream,
    format_cpuset,
    stream_stdout_and_stderr,
)


def test_mapped_process_stream_owns_unstarted_mapping() -> None:
    child = stream_stdout_and_stderr([sys.executable, "-c", "input()"], pipe_stdin=True)
    stream = MappedProcessStream(child, (line for _, line in child))

    with stream as entered:
        assert entered is stream

    stream.close()
    assert list(stream) == []
    _assert_process_released(child)


@pytest.mark.parametrize("strict,marked", [(True, True), (True, False), (False, True)])
@pytest.mark.parametrize("raw", [False, True])
def test_captured_workload_failure_classification(
    strict: bool, marked: bool, raw: bool
) -> None:
    config = ClientConfig(
        client_call=[sys.executable], strict_workload_errors=strict, raw_diagnostics=raw
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import sys; print('out'); print('Error: No such container: secret', file=sys.stderr); sys.exit(125)",
    ]
    with pytest.raises(DockerException) as raised:
        pywhales.utils.run(command, workload_status=marked)
    error = raised.value
    assert type(error) is (DockerException if strict and marked else NoSuchContainer)
    assert error.docker_command == command
    assert error.return_code == 125
    assert error.stdout == "out\n"
    assert error.stderr == "Error: No such container: secret\n"
    assert ("secret" in str(error)) is raw


@pytest.mark.parametrize("strict,marked", [(True, True), (True, False), (False, True)])
@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("raw", [False, True])
def test_stream_workload_failure_classification(
    strict: bool, marked: bool, callback: bool, raw: bool
) -> None:
    config = ClientConfig(
        client_call=[sys.executable], strict_workload_errors=strict, raw_diagnostics=raw
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import sys; print('Error: No such container: secret', file=sys.stderr, flush=True); input(); sys.exit(125)",
    ]
    stream = stream_stdout_and_stderr(command, pipe_stdin=True, workload_status=marked)
    errors: list[OSError | BufferError | DockerException] = []
    notified = Event()
    try:
        assert next(stream) == ("stderr", b"Error: No such container: secret\n")
        if callback:

            def notify(error: OSError | BufferError | DockerException) -> None:
                errors.append(error)
                notified.set()

            stream._on_failure(notify)
        config.strict_workload_errors = not strict
        config.raw_diagnostics = not raw
        assert stream.stdin is not None
        stream.stdin.write(b"go\n")
        stream.stdin.flush()
        if callback:
            assert notified.wait(3)
        with pytest.raises(DockerException) as raised:
            list(stream)
        error = raised.value
        assert type(error) is (
            DockerException if strict and marked else NoSuchContainer
        )
        assert error.docker_command == command
        assert error.return_code == 125
        assert error.stdout is None
        assert error.stderr == "Error: No such container: secret\n"
        assert ("secret" in str(error)) is raw
        if callback:
            assert errors == [error]
    finally:
        stream.close()


def test_format_cpuset_none() -> None:
    assert format_cpuset(None) is None


def test_format_cpuset_list_of_ints() -> None:
    assert format_cpuset([0, 2, 3]) == "0,2,3"


def test_format_cpuset_empty_list() -> None:
    assert format_cpuset([]) == ""


def test_format_cpuset_string_passthrough() -> None:
    # cpuset strings accepted by the CLI must be passed through unmodified so
    # ranges like "0-3" and mixes like "0-3,7" reach docker as-is.
    assert format_cpuset("0-3") == "0-3"
    assert format_cpuset("0,2") == "0,2"
    assert format_cpuset("0-3,7") == "0-3,7"


def test_format_cpuset_generator() -> None:
    assert format_cpuset(iter([1, 4])) == "1,4"


def test_environment_variables_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_VARIABLE", "dododada")

    stdout = pywhales.utils.run(
        ["bash", "-c", "echo $SOME_VARIABLE && echo $OTHER_VARIABLE"],
        capture_stdout=True,
        env={"OTHER_VARIABLE": "dudu"},
    )
    assert stdout == "dododada\ndudu"


def test_stream_stdout_and_stderr_interactive() -> None:
    proc = stream_stdout_and_stderr(["cat"], pipe_stdin=True)
    assert isinstance(proc, ProcessStream)
    assert proc.stdin is not None
    proc.stdin.write(b"hello world\n")
    proc.stdin.close()
    output = list(proc)
    assert ("stdout", b"hello world\n") in output


def test_stream_stdout_and_stderr_interactive_multiple_lines() -> None:
    proc = stream_stdout_and_stderr(["cat"], pipe_stdin=True)
    assert isinstance(proc, ProcessStream)
    assert proc.stdin is not None
    proc.stdin.write(b"line1\n")
    proc.stdin.write(b"line2\n")
    proc.stdin.close()
    output = list(proc)
    stdout_lines = [line for source, line in output if source == "stdout"]
    assert b"line1\n" in stdout_lines
    assert b"line2\n" in stdout_lines


def test_stream_stdout_and_stderr_non_interactive() -> None:
    """Leave stdin unavailable when streaming without an interactive input pipe."""
    result = stream_stdout_and_stderr(["echo", "hi"])
    assert isinstance(result, ProcessStream)
    assert result.stdin is None
    output = list(result)
    assert ("stdout", b"hi\n") in output


def test_process_stream_close_after_eof() -> None:
    stream = stream_stdout_and_stderr([sys.executable, "-c", "print('done')"])
    assert list(stream) == [("stdout", b"done\n")]

    stream.close()
    stream.close()

    assert list(stream) == []


def _assert_process_released(stream: ProcessStream) -> None:
    """Check child reaping, worker shutdown, and wake-descriptor and pipe closure."""
    assert stream._process.poll() is not None
    assert all(not thread.is_alive() for thread in stream._threads)
    assert not stream._reaper.is_alive()
    assert not stream._watchdog.is_alive()
    assert stream._stop.fileno() is None
    assert all(
        pipe.closed
        for pipe in (stream.stdin, stream._process.stdout, stream._process.stderr)
        if pipe is not None
    )


def test_client_stream_budget_is_live_for_new_launches_but_frozen_after_start() -> None:
    config = ClientConfig(client_call=[sys.executable], stream_buffer_bytes=3)
    command = [
        *config.docker_cmd,
        "-c",
        "input(); print('long-record', flush=True); input()",
    ]
    stream = stream_stdout_and_stderr(command, pipe_stdin=True)
    config.stream_buffer_bytes = None
    try:
        assert stream.stdin is not None
        stream.stdin.write(b"go\n")
        stream.stdin.flush()
        with pytest.raises(BufferError):
            next(stream)
        _assert_process_released(stream)
        with stream_stdout_and_stderr(command, pipe_stdin=True) as fresh:
            assert fresh.stdin is not None
            fresh.stdin.write(b"go\n")
            fresh.stdin.flush()
            assert next(fresh) == ("stdout", b"long-record\n")
        _assert_process_released(fresh)
    finally:
        stream.close()


def test_explicit_unbounded_buffer_does_not_fall_back_to_client_budget() -> None:
    config = ClientConfig(client_call=[sys.executable], stream_buffer_bytes=3)

    stream = stream_stdout_and_stderr(
        [*config.docker_cmd, "-c", "print('long-record')"], buffer_bytes=None
    )

    assert list(stream) == [("stdout", b"long-record\n")]
    _assert_process_released(stream)


def test_buildx_mapped_stream_applies_client_budget_without_wrapper_override() -> None:
    config = ClientConfig(client_call=[sys.executable], stream_buffer_bytes=3)

    stream = stream_buildx_logs([*config.docker_cmd, "-c", "print('oversized')"])

    with pytest.raises(BufferError):
        list(stream)
    _assert_process_released(stream._stream)


@pytest.mark.parametrize("raw", [False, True])
def test_run_timeout_keeps_client_diagnostics_and_raw_output(raw: bool) -> None:
    secret = "timeout-secret-sentinel"
    config = ClientConfig(client_call=[sys.executable], raw_diagnostics=raw)

    with pytest.raises(DockerTimeout) as raised:
        pywhales.utils.run(
            [
                *config.docker_cmd,
                "-c",
                f"print({secret!r}, flush=True); __import__('threading').Event().wait()",
            ],
            input=b"",
            timeout=0.5,
        )

    assert (secret in str(raised.value)) is raw
    assert raised.value.stdout == secret + "\n"


def test_mapped_client_stream_applies_backpressure_and_cancels_saturated_child() -> (
    None
):
    config = ClientConfig(client_call=[sys.executable], stream_buffer_bytes=3)
    child = stream_stdout_and_stderr(
        [
            *config.docker_cmd,
            "-c",
            "import os; os.write(1, b'ab\\n' * 100000); input()",
        ],
        pipe_stdin=True,
    )
    stream = MappedProcessStream(child, (line for _, line in child))
    try:
        with child._queue.condition:
            assert child._queue.condition.wait_for(
                lambda: child._queue._bytes == 3 and bool(child._inflight["stdout"]),
                timeout=3,
            )

        assert next(stream) == b"ab\n"
        stream.close()

        _assert_process_released(child)
    finally:
        stream.close()


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("failure", ["natural", "owner", "timeout"])
def test_stream_diagnostics_snapshot_client_policy_at_launch(
    raw: bool, failure: str
) -> None:
    secret = "stream-secret-sentinel"
    config = ClientConfig(client_call=[sys.executable], raw_diagnostics=raw)
    action = "input()" if failure == "timeout" else "raise SystemExit(7)"
    command = [
        *config.docker_cmd,
        "-c",
        f"import sys; print({secret!r}, file=sys.stderr, flush=True); input(); {action}",
    ]
    stream = stream_stdout_and_stderr(
        command, pipe_stdin=True, timeout=1 if failure == "timeout" else None
    )
    errors: list[OSError | BufferError | DockerException] = []
    if failure == "owner":
        stream._on_failure(errors.append)
    config.raw_diagnostics = not raw
    try:
        assert next(stream) == ("stderr", (secret + "\n").encode())
        assert stream.stdin is not None
        stream.stdin.write(b"go\n")
        stream.stdin.flush()
        expected = DockerTimeout if failure == "timeout" else DockerException
        with pytest.raises(expected) as raised:
            list(stream)

        assert (secret in str(raised.value)) is raw
        assert raised.value.stderr == secret + "\n"
        if failure == "owner":
            assert errors == [raised.value]
        _assert_process_released(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("consume", [False, True])
def test_process_stream_closes_idle_child(consume: bool) -> None:
    with stream_stdout_and_stderr(
        [sys.executable, "-c", "print('ready', flush=True); input()"], pipe_stdin=True
    ) as stream:
        if consume:
            assert next(stream) == ("stdout", b"ready\n")

        started = monotonic()
        stream.close()
        stream.close()

        assert monotonic() - started < 3
        assert list(stream) == []
        _assert_process_released(stream)


def test_process_stream_closes_with_pending_output() -> None:
    with stream_stdout_and_stderr(
        [sys.executable, "-c", "print('queued\\n' * 100000, flush=True); input()"],
        pipe_stdin=True,
    ) as stream:
        assert stream._queue.get(timeout=3) == ("stdout", b"queued\n")

        started = monotonic()
        stream.close()

        assert monotonic() - started < 3
        _assert_process_released(stream)


def test_process_stream_context_closes_before_iteration() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True
    )

    with stream as entered:
        assert entered is stream

    _assert_process_released(stream)


def test_process_stream_context_preserves_body_error() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "print('ready', flush=True); input()"], pipe_stdin=True
    )
    error = RuntimeError("consumer failed")

    with pytest.raises(RuntimeError) as raised, stream:
        assert next(stream) == ("stdout", b"ready\n")
        raise error

    assert raised.value is error
    _assert_process_released(stream)


def test_process_stream_close_interrupts_pending_next() -> None:
    with stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True
    ) as stream:
        entered = Event()
        finished = Event()
        output: list[tuple[str, bytes]] = []

        def consume() -> None:
            entered.set()
            output.extend(stream)
            finished.set()

        consumer = Thread(target=consume, daemon=True)
        consumer.start()
        assert entered.wait(timeout=3)

        stream.close()

        assert finished.wait(timeout=3)
        consumer.join(timeout=3)
        assert output == []
        _assert_process_released(stream)


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM can only be ignored on POSIX"
)
def test_process_stream_kills_child_that_ignores_termination() -> None:
    with stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); input()",
        ],
        pipe_stdin=True,
    ) as stream:
        assert next(stream) == ("stdout", b"ready\n")

        started = monotonic()
        stream.close()

        assert monotonic() - started < 3
        _assert_process_released(stream)


def test_process_stream_eof_preserves_pipe_order_and_unterminated_lines() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; "
            "[(os.write(1, f'out{n}\\n'.encode()), os.write(2, f'err{n}\\n'.encode())) "
            "for n in range(1000)]; os.write(1, b'tail'); os.write(2, b'error tail')",
        ]
    )

    output = list(stream)

    assert [line for origin, line in output if origin == "stdout"] == [
        *(f"out{number}\n".encode() for number in range(1000)),
        b"tail",
    ]
    assert [line for origin, line in output if origin == "stderr"] == [
        *(f"err{number}\n".encode() for number in range(1000)),
        b"error tail",
    ]
    _assert_process_released(stream)


def test_process_stream_eof_preserves_command_failure() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import sys; print('output'); "
            "sys.stderr.write('No such container: missing\\n'); sys.exit(7)",
        ],
        pipe_stdin=True,
    )
    output: list[tuple[str, bytes]] = []

    with pytest.raises(NoSuchContainer) as raised:
        output.extend(stream)

    assert ("stdout", b"output\n") in output
    assert ("stderr", b"No such container: missing\n") in output
    assert raised.value.return_code == 7
    assert raised.value.stderr == "No such container: missing\n"
    _assert_process_released(stream)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handlers")
def test_process_stream_close_never_runs_signal_handler(tmp_path: Path) -> None:
    forwarded = tmp_path / "forwarded"
    with stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import signal, sys; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, "
            "lambda *_: Path(sys.argv[1]).write_bytes(b'forwarded')); "
            "print('ready', flush=True)\n"
            "while True: signal.pause()",
            forwarded,
        ]
    ) as stream:
        assert stream._queue.get(timeout=3) == ("stdout", b"ready\n")

        stream.close()

        assert not forwarded.exists()
        assert stream._process.returncode == -signal.SIGKILL
        _assert_process_released(stream)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
def test_process_stream_close_discards_buffered_stdin_held_by_descendant() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    with (
        os.fdopen(release_read, "rb", buffering=0) as release_reader,
        os.fdopen(release_write, "wb", buffering=0) as release_writer,
        os.fdopen(status_read, "rb", buffering=0) as status_reader,
        os.fdopen(status_write, "wb", buffering=0) as status_writer,
    ):
        stream = stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                "import os, signal, sys\n"
                "release, status = map(int, sys.argv[1:])\n"
                "if os.fork() == 0:\n"
                "    os.write(status, b'R')\n"
                "    os.read(release, 1)\n"
                "    os._exit(0)\n"
                "os.close(release)\n"
                "os.close(status)\n"
                "signal.pause()\n",
                release_reader.fileno(),
                status_writer.fileno(),
            ],
            pass_fds=(release_reader.fileno(), status_writer.fileno()),
            pipe_stdin=True,
        )
        closer = Thread(target=stream.close, daemon=True)
        try:
            release_reader.close()
            status_writer.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b"R"
            assert stream.stdin is not None
            stdin_fd = stream.stdin.fileno()
            os.set_blocking(stdin_fd, False)
            with pytest.raises(BlockingIOError):
                while True:
                    os.write(stdin_fd, b"x" * 4096)
            assert stream.stdin.write(b"buffered") == len(b"buffered")
            os.set_blocking(stdin_fd, True)

            closer.start()
            closer.join(timeout=3)

            assert not closer.is_alive(), "close blocked flushing buffered stdin"
            _assert_process_released(stream)
            assert not select.select([status_reader], [], [], 0)[0]
        finally:
            release_writer.close()
            if closer.ident is None:
                closer.start()
            closer.join(timeout=3)
            assert not closer.is_alive()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b""
            _assert_process_released(stream)


def test_run_timeout_preserves_captured_output() -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('ready', flush=True); "
        "sys.stderr.write('error\\n'); sys.stderr.flush(); "
        "__import__('threading').Event().wait()",
    ]

    with pytest.raises(DockerTimeout) as raised:
        pywhales.utils.run(command, input=b"", timeout=1)

    assert type(raised.value).__name__ == "DockerTimeout"
    assert raised.value.docker_command == command
    assert raised.value.stdout == "ready\n"
    assert raised.value.stderr == "error\n"


def test_default_timeout_is_selected_at_each_launch_and_explicit_replaces_it() -> None:
    config = ClientConfig(client_call=[sys.executable], default_timeout=0.1)
    command = [*config.docker_cmd, "-c", "import time; time.sleep(0.3); print('ok')"]
    with pytest.raises(DockerTimeout):
        pywhales.utils.run(command)
    assert pywhales.utils.run(command, timeout=2) == "ok"
    config.default_timeout = 0
    assert pywhales.utils.run(command, timeout=2) == "ok"
    with pytest.raises(ValueError):
        pywhales.utils.run(command)


def test_default_timeout_applies_to_idle_owned_stream() -> None:
    config = ClientConfig(client_call=[sys.executable], default_timeout=0.1)
    stream = stream_stdout_and_stderr(
        [*config.docker_cmd, "-c", "input()"], pipe_stdin=True
    )
    assert stream._released.wait(timeout=3)
    with pytest.raises(DockerTimeout):
        next(stream)
    assert list(stream) == []


def test_default_timeout_applies_to_captured_stdin() -> None:
    config = ClientConfig(client_call=[sys.executable], default_timeout=0.1)
    command = [
        *config.docker_cmd,
        "-c",
        "import sys,time; sys.stdin.read(); time.sleep(3)",
    ]
    with pytest.raises(DockerTimeout):
        pywhales.utils.run(command, input=b"payload")


def test_default_timeout_applies_to_tty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ClientConfig(client_call=[sys.executable], default_timeout=0.1)
    command = [*config.docker_cmd, "-c", "import time; time.sleep(3)"]
    with (
        (tmp_path / "terminal.txt").open("w") as terminal,
        monkeypatch.context() as patch,
    ):
        patch.setattr(sys, "stdout", terminal)
        patch.setattr(sys, "stderr", terminal)
        with pytest.raises(DockerTimeout):
            pywhales.utils.run(command, tty=True)


def test_stream_explicit_timeout_replaces_invalid_default() -> None:
    config = ClientConfig(client_call=[sys.executable])
    config.default_timeout = 0
    command = [*config.docker_cmd, "-c", "print('ok')"]
    assert list(stream_stdout_and_stderr(command, timeout=3)) == [("stdout", b"ok\n")]
    with pytest.raises(ValueError):
        stream_stdout_and_stderr(command)


def test_default_timeout_is_used_inside_observation() -> None:
    config = ClientConfig(client_call=[sys.executable], default_timeout=0.1)
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    with observation(policy), pytest.raises(DockerTimeout):
        pywhales.utils.run([*config.docker_cmd, "-c", "import time; time.sleep(3)"])


def test_invalid_selected_default_is_not_an_observation_environment_error() -> None:
    config = ClientConfig(client_call=[sys.executable])
    config.default_timeout = 0
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    with observation(policy), pytest.raises(ValueError):
        pywhales.utils.run([*config.docker_cmd, "-c", "pass"])


def test_captured_input_needs_a_stdin_budget_even_under_an_aggregate_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError("a process was launched")

    monkeypatch.setattr("pywhales._bounded_capture.subprocess.Popen", launch)
    policy = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_io_bytes=1024,
    )
    with observation(policy), pytest.raises(ValueError):
        pywhales.utils.run([sys.executable, "-c", "pass"], input=b"x")


def test_process_stream_times_out_without_consumption() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )

    assert stream._released.wait(timeout=3)

    with pytest.raises(DockerTimeout) as raised:
        next(stream)
    assert raised.value.docker_command[:2] == [sys.executable, "-c"]
    assert list(stream) == []
    _assert_process_released(stream)


@pytest.mark.parametrize("mapped", [False, True])
def test_process_stream_context_reports_unconsumed_timeout(mapped: bool) -> None:
    child = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )
    stream = (
        MappedProcessStream(child, (line for _, line in child)) if mapped else child
    )

    with pytest.raises(DockerTimeout), stream:
        assert child._released.wait(timeout=3)

    assert list(stream) == []
    _assert_process_released(child)


@pytest.mark.parametrize(
    "timeout", [0, -1, float("inf"), float("-inf"), float("nan"), True]
)
def test_invalid_timeout_is_rejected_before_launch(timeout: float) -> None:
    with pytest.raises(ValueError):
        pywhales.utils.run(["does-not-exist"], timeout=timeout)
    with pytest.raises(ValueError):
        stream_stdout_and_stderr(["does-not-exist"], timeout=timeout)


@pytest.mark.parametrize("timeout", [None, 1.0, 1e308])
def test_run_deadline_preserves_return_shapes(timeout: float | None) -> None:
    assert (
        pywhales.utils.run([sys.executable, "-c", "print('output')"], timeout=timeout)
        == "output"
    )
    assert pywhales.utils.run(
        [
            sys.executable,
            "-c",
            "import sys; print('output'); sys.stderr.write('error\\n')",
        ],
        return_stderr=True,
        timeout=timeout,
    ) == ("output", "error")


def test_run_natural_failure_is_not_timeout() -> None:
    with pytest.raises(DockerException) as raised:
        pywhales.utils.run([sys.executable, "-c", "raise SystemExit(7)"], timeout=1)
    assert type(raised.value) is DockerException
    assert raised.value.return_code == 7


def test_process_stream_deadline_continues_while_consumption_pauses() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "print('ready', flush=True); input()"],
        pipe_stdin=True,
        timeout=1,
    )
    assert next(stream) == ("stdout", b"ready\n")

    assert stream._released.wait(timeout=3)

    with pytest.raises(DockerTimeout):
        next(stream)
    _assert_process_released(stream)


@pytest.mark.parametrize("mapped", [False, True])
def test_process_stream_timeout_preserves_active_body_exception(mapped: bool) -> None:
    child = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )
    stream = (
        MappedProcessStream(child, (line for _, line in child)) if mapped else child
    )
    error = RuntimeError("body failure")

    with pytest.raises(RuntimeError) as raised, stream:
        assert child._released.wait(timeout=3)
        raise error

    assert raised.value is error
    _assert_process_released(child)


def test_process_stream_explicit_close_cancels_deadline() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=60
    )

    stream.close()

    with stream:
        assert list(stream) == []
    _assert_process_released(stream)


def test_process_stream_completed_child_keeps_unread_output_past_deadline() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; os.write(1, b'ab\\n' * 100)"],
        timeout=0.5,
        buffer_bytes=3,
    )
    try:
        assert stream._process.wait(timeout=3) == 0
        assert not stream._released.wait(timeout=0.6)

        assert list(stream) == [("stdout", b"ab\n")] * 100

        _assert_process_released(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("payload", [b"1234567\n", b"12345678"])
@pytest.mark.parametrize("pipe", [1, 2])
def test_process_stream_record_at_budget_is_preserved(
    payload: bytes, pipe: int
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", f"import os; os.write({pipe}, {payload!r})"],
        buffer_bytes=8,
    )

    assert list(stream) == [("stdout" if pipe == 1 else "stderr", payload)]

    _assert_process_released(stream)


@pytest.mark.parametrize("payload", [b"12345678\n", b"123456789"])
@pytest.mark.parametrize("pipe", [1, 2])
def test_process_stream_oversize_record_is_observable_without_consumer(
    payload: bytes, pipe: int
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", f"import os; os.write({pipe}, {payload!r}); input()"],
        pipe_stdin=True,
        buffer_bytes=8,
    )
    try:
        assert stream._released.wait(timeout=3)

        with pytest.raises(BufferError):
            next(stream)

        assert list(stream) == []
        _assert_process_released(stream)
    finally:
        stream.close()


def test_process_stream_stderr_history_has_independent_budget() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; os.write(2, b'ab\\n'); input(); os.write(2, b'cd\\n'); input()",
        ],
        pipe_stdin=True,
        buffer_bytes=4,
    )
    try:
        assert next(stream) == ("stderr", b"ab\n")
        assert stream.stdin is not None
        stream.stdin.write(b"continue\n")
        stream.stdin.flush()

        with pytest.raises(BufferError):
            next(stream)

        assert len(stream._stderr) <= 4
        _assert_process_released(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("timeout", [None, 0.3])
def test_process_stream_saturated_queue_wakes_on_close_or_expiry(
    timeout: float | None,
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; os.write(1, b'ab\\n' * 100000); input()"],
        pipe_stdin=True,
        timeout=timeout,
        buffer_bytes=3,
    )
    try:
        with stream._queue.condition:
            assert stream._queue.condition.wait_for(
                lambda: stream._queue._bytes == 3, timeout=3
            )
            assert max(stream._pending_bytes.values()) <= 3
            assert len(stream._stderr) <= 3
        if timeout is None:
            closers = [Thread(target=stream.close, daemon=True) for _ in range(4)]
            for closer in closers:
                closer.start()
            for closer in closers:
                closer.join(timeout=3)
                assert not closer.is_alive()
            assert list(stream) == []
        else:
            assert stream._released.wait(timeout=3)
            with pytest.raises(DockerTimeout):
                next(stream)
        _assert_process_released(stream)
    finally:
        stream.close()


def test_process_stream_unfinished_records_have_separate_pipe_budgets() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'ab\\n1234'); os.write(2, b'5678'); input()",
        ],
        pipe_stdin=True,
        buffer_bytes=4,
    )
    try:
        with stream._queue.condition:
            assert stream._queue.condition.wait_for(
                lambda: stream._pending_bytes == {"stdout": 4, "stderr": 4}, timeout=3
            )
            assert stream._queue._bytes == 3
            assert len(stream._stderr) == 4

        assert next(stream) == ("stdout", b"ab\n")
    finally:
        stream.close()
    _assert_process_released(stream)


def test_process_stream_buffer_error_is_reported_on_context_exit() -> None:
    child = stream_stdout_and_stderr(
        [sys.executable, "-c", "print('oversize'); input()"],
        pipe_stdin=True,
        buffer_bytes=3,
    )
    stream = MappedProcessStream(child, (line for _, line in child))

    with pytest.raises(BufferError), stream:
        assert child._released.wait(timeout=3)

    _assert_process_released(child)


def test_process_stream_timeout_preserves_partial_output() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'partial\\xff'); os.write(2, b'error'); input()",
        ],
        pipe_stdin=True,
        timeout=1,
        buffer_bytes=16,
    )
    try:
        assert stream._released.wait(timeout=3)

        with pytest.raises(DockerTimeout) as raised:
            next(stream)

        assert raised.value.stdout == "partial\ufffd"
        assert raised.value.stderr == "error"
        _assert_process_released(stream)
    finally:
        stream.close()


def test_process_stream_failure_wakes_sibling_blocked_on_full_queue() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'ab\\n' * 10); input(); os.write(2, b'oversize'); input()",
        ],
        pipe_stdin=True,
        buffer_bytes=3,
    )
    try:
        with stream._queue.condition:
            assert stream._queue.condition.wait_for(
                lambda: stream._queue._bytes == 3, timeout=3
            )
        assert stream.stdin is not None
        stream.stdin.write(b"fail\n")
        stream.stdin.flush()

        assert stream._released.wait(timeout=3)

        with pytest.raises(BufferError):
            next(stream)
        _assert_process_released(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("budget", [0, -1, True, False, 1.5, "3"])
def test_process_stream_invalid_budget_is_rejected_before_launch(budget: int) -> None:
    with pytest.raises(ValueError):
        stream_stdout_and_stderr(["does-not-exist"], buffer_bytes=budget)


def test_process_stream_close_after_timeout_does_not_erase_error() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )
    assert stream._released.wait(timeout=3)

    stream.close()

    with pytest.raises(DockerTimeout):
        next(stream)
    with stream:
        assert list(stream) == []
    _assert_process_released(stream)


def test_process_stream_timeout_interrupts_blocked_next() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )
    errors: list[DockerTimeout] = []

    def consume() -> None:
        try:
            next(stream)
        except DockerTimeout as error:
            errors.append(error)

    consumer = Thread(target=consume, daemon=True)
    consumer.start()
    consumer.join(timeout=3)

    assert not consumer.is_alive()
    assert len(errors) == 1
    _assert_process_released(stream)


def test_mapped_process_stream_mapper_failure_closes_saturated_transport() -> None:
    child = stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; os.write(1, b'ab\\n' * 100000); input()"],
        pipe_stdin=True,
        buffer_bytes=3,
    )
    error = ValueError("mapper failed")

    def map_record(record: tuple[str, bytes]) -> bytes:
        raise error

    stream = MappedProcessStream(child, map(map_record, child))

    with pytest.raises(ValueError) as raised:
        next(stream)

    assert raised.value is error
    _assert_process_released(child)


def test_mapped_process_stream_close_after_expiry_keeps_pending_timeout() -> None:
    child = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=0.1
    )
    stream = MappedProcessStream(child, (line for _, line in child))
    assert child._released.wait(timeout=3)

    stream.close()

    with pytest.raises(DockerTimeout):
        next(stream)
    assert list(stream) == []
    _assert_process_released(child)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
def test_process_stream_close_releases_pipes_inherited_after_natural_exit() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    with (
        os.fdopen(release_read, "rb", buffering=0) as release_reader,
        os.fdopen(release_write, "wb", buffering=0) as release_writer,
        os.fdopen(status_read, "rb", buffering=0) as status_reader,
        os.fdopen(status_write, "wb", buffering=0) as status_writer,
    ):
        stream = stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                "import os, sys\n"
                "release, status = map(int, sys.argv[1:])\n"
                "if os.fork() == 0:\n"
                "    os.write(status, b'R')\n"
                "    os.read(release, 1)\n"
                "    os._exit(0)\n"
                "os._exit(0)\n",
                release_reader.fileno(),
                status_writer.fileno(),
            ],
            pass_fds=(release_reader.fileno(), status_writer.fileno()),
            timeout=0.5,
        )
        try:
            release_reader.close()
            status_writer.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b"R"
            assert stream._process.wait(timeout=3) == 0
            assert not stream._released.wait(timeout=0.6)

            started = monotonic()
            stream.close()

            assert monotonic() - started < 3
            assert list(stream) == []
            _assert_process_released(stream)
        finally:
            release_writer.close()
            stream.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b""


def test_process_stream_deadline_uses_launch_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pywhales.utils, "monotonic", lambda: monotonic() - 60)

    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=30
    )

    assert stream._released.wait(timeout=3)
    with pytest.raises(DockerTimeout):
        next(stream)
    _assert_process_released(stream)


def test_process_stream_close_wins_pending_natural_failure_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = stream_stdout_and_stderr([sys.executable, "-c", "raise SystemExit(7)"])
    reaped = Event()
    resume = Event()
    original_raise = stream._raise_error
    errors: list[DockerException] = []

    def pause_after_reap() -> None:
        original_raise()
        reaped.set()
        assert resume.wait(timeout=3)

    def consume() -> None:
        try:
            list(stream)
        except DockerException as error:
            errors.append(error)

    monkeypatch.setattr(stream, "_raise_error", pause_after_reap)
    consumer = Thread(target=consume, daemon=True)
    consumer.start()
    try:
        assert reaped.wait(timeout=3)

        stream.close()
        resume.set()
        consumer.join(timeout=3)

        assert not consumer.is_alive()
        assert errors == []
        _assert_process_released(stream)
    finally:
        resume.set()
        consumer.join(timeout=3)
        stream.close()


def test_process_stream_close_after_deadline_records_expiry() -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=60
    )
    with stream._queue.condition:
        stream._deadline = monotonic() - 1

    stream.close()

    with pytest.raises(DockerTimeout):
        next(stream)
    _assert_process_released(stream)


def test_process_stream_close_wins_before_owner_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; input(); os._exit(7)"], pipe_stdin=True
    )
    observed = Event()
    resume = Event()
    errors: list[OSError | BufferError | DockerException] = []
    fail = stream._fail

    def pause_before_failure(error: OSError | BufferError | DockerException) -> None:
        observed.set()
        assert resume.wait(timeout=3)
        fail(error)

    monkeypatch.setattr(stream, "_fail", pause_before_failure)
    stream._on_failure(errors.append)
    closer = Thread(target=stream.close, daemon=True)
    try:
        assert stream.stdin is not None
        stream.stdin.write(b"exit\n")
        stream.stdin.flush()
        assert observed.wait(timeout=3)

        closer.start()
        assert stream._stop.wait(timeout=3)
        resume.set()
        closer.join(timeout=3)

        assert not closer.is_alive()
        assert errors == []
        assert list(stream) == []
        _assert_process_released(stream)
    finally:
        resume.set()
        if closer.ident is not None:
            closer.join(timeout=3)
        stream.close()


def test_process_stream_owner_keeps_natural_failure_exception_type() -> None:
    message = b"No such container: missing\n"
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            f"import os; os.write(2, {message!r}); input(); os._exit(7)",
        ],
        pipe_stdin=True,
    )
    errors: list[OSError | BufferError | DockerException] = []
    stream._on_failure(errors.append)
    try:
        assert next(stream) == ("stderr", message)

        assert stream.stdin is not None
        stream.stdin.write(b"exit\n")
        stream.stdin.flush()

        assert stream._released.wait(timeout=3)
        assert len(errors) == 1
        with pytest.raises(NoSuchContainer) as raised:
            next(stream)
        assert raised.value is errors[0]
        assert raised.value.return_code == 7
        assert raised.value.stderr == message.decode()
        assert list(stream) == []
        _assert_process_released(stream)
    finally:
        stream.close()


def test_process_stream_timeout_preserves_read_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input(); print('captured', flush=True); input()"],
        pipe_stdin=True,
        timeout=60,
    )
    read = Event()
    resume = Event()
    check_size = stream._check_size

    def pause_after_read(size: int, category: str) -> None:
        check_size(size, category)
        read.set()
        assert resume.wait(timeout=3)

    monkeypatch.setattr(stream, "_check_size", pause_after_read)
    try:
        assert stream.stdin is not None
        stream.stdin.write(b"read\n")
        stream.stdin.flush()
        assert read.wait(timeout=3)
        with stream._queue.condition:
            stream._deadline = monotonic() - 1
            stream._expire()

        resume.set()
        assert stream._released.wait(timeout=3)

        with pytest.raises(DockerTimeout) as raised:
            next(stream)
        assert raised.value.stdout == "captured\n"
        _assert_process_released(stream)
    finally:
        resume.set()
        stream.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
def test_run_completed_child_with_inherited_pipes_is_not_timeout() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    with (
        os.fdopen(release_read, "rb", buffering=0) as release_reader,
        os.fdopen(release_write, "wb", buffering=0) as release_writer,
        os.fdopen(status_read, "rb", buffering=0) as status_reader,
        os.fdopen(status_write, "wb", buffering=0) as status_writer,
    ):
        try:
            output = pywhales.utils.run(
                [
                    sys.executable,
                    "-c",
                    "import os, sys\n"
                    "release, status = map(int, sys.argv[1:])\n"
                    "if os.fork() == 0:\n"
                    "    os.write(status, b'R')\n"
                    "    os.read(release, 1)\n"
                    "    os._exit(0)\n"
                    "os.write(1, b'completed\\n')\n"
                    "os._exit(0)\n",
                    release_reader.fileno(),
                    status_writer.fileno(),
                ],
                pass_fds=(release_reader.fileno(), status_writer.fileno()),
                timeout=0.5,
            )

            assert output == "completed"
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b"R"
        finally:
            release_reader.close()
            status_writer.close()
            release_writer.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b""


def test_process_stream_queue_budget_is_shared_between_both_producers() -> None:
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'aa\\n' * 10); input(); os.write(2, b'bb\\n'); input()",
        ],
        pipe_stdin=True,
        buffer_bytes=3,
    )
    try:
        with stream._queue.condition:
            assert stream._queue.condition.wait_for(
                lambda: stream._queue._bytes == 3, timeout=3
            )
        assert stream.stdin is not None
        stream.stdin.write(b"stderr\n")
        stream.stdin.flush()
        with stream._queue.condition:
            assert stream._queue.condition.wait_for(
                lambda: all(stream._inflight.values()), timeout=3
            )
            assert stream._queue._bytes == 3
            assert len(stream._stderr) == 3
    finally:
        stream.close()
    _assert_process_released(stream)


@pytest.mark.parametrize("budget", [65536, 65537])
def test_process_stream_record_bound_includes_newline_across_chunks(
    budget: int,
) -> None:
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; os.write(1, b'x' * 65536 + b'\\n')"],
        buffer_bytes=budget,
    )
    try:
        if budget == 65536:
            with pytest.raises(BufferError):
                list(stream)
        else:
            assert list(stream) == [("stdout", b"x" * 65536 + b"\n")]
        _assert_process_released(stream)
    finally:
        stream.close()


@dataclass
class WaitLog:
    """Timed waits and condition waits made by threads other than the main one."""

    timed: list[tuple[int, float]] = field(default_factory=list)
    conditions: Counter[int] = field(default_factory=Counter)

    def timeouts(self, owned: set[int | None]) -> list[float]:
        return [timeout for ident, timeout in self.timed if ident in owned]


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> WaitLog:
    """Record worker-thread timeouts and turn them into untimed waits.

    A worker that depends on a timer to make progress stalls, so tests prove
    both that no timer is requested and that progress never needed one.
    """
    log = WaitLog()
    event_wait = threading.Event.wait
    condition_wait = threading.Condition.wait

    def untimed(timeout: float | None) -> float | None:
        if timeout is None or threading.current_thread() is threading.main_thread():
            return timeout
        log.timed.append((threading.get_ident(), timeout))
        return None

    def event(self: threading.Event, timeout: float | None = None) -> bool:
        return event_wait(self, untimed(timeout))

    def condition(self: threading.Condition, timeout: float | None = None) -> bool:
        if threading.current_thread() is not threading.main_thread():
            log.conditions[threading.get_ident()] += 1
        return condition_wait(self, untimed(timeout))

    monkeypatch.setattr(threading.Event, "wait", event)
    monkeypatch.setattr(threading.Condition, "wait", condition)
    return log


def _next_within(stream: ProcessStream, seconds: float = 3) -> tuple[str, bytes] | None:
    """Return the next record, or None if delivery is still pending after a bound."""
    records: list[tuple[str, bytes]] = []

    def pull() -> None:
        try:
            records.append(next(stream))
        except StopIteration, DockerException:
            return

    puller = Thread(target=pull, daemon=True)
    puller.start()
    puller.join(seconds)
    return records[0] if records else None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_idle_process_stream_has_no_timers_and_delivers_each_line(
    waits: WaitLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an idle stream with no deadline and a child that echoes stdin lines.
    reads: Counter[int] = Counter()
    read = os.read

    def counted(fd: int, size: int) -> bytes:
        reads[fd] += 1
        return read(fd, size)

    monkeypatch.setattr(os, "read", counted)
    before = set(threading.enumerate())
    stream = stream_stdout_and_stderr(
        [
            sys.executable,
            "-c",
            "import sys\nfor line in sys.stdin: print(line, end='', flush=True)",
        ],
        pipe_stdin=True,
    )
    owned = {thread.ident for thread in set(threading.enumerate()) - before}
    assert stream._process.stdout is not None and stream._process.stderr is not None
    pipes = {stream._process.stdout.fileno(), stream._process.stderr.fileno()}
    try:
        # When nothing is written for a while.
        sleep(0.4)

        # Then no worker read, woke, or requested a timer on its own.
        assert sum(reads[fd] for fd in pipes) <= 4
        assert waits.timeouts(owned) == []
        assert {
            ident: count for ident, count in waits.conditions.items() if ident in owned
        } == {stream._watchdog.ident: 1}

        # And each line is delivered although no timer could have woken a reader.
        assert stream.stdin is not None
        for number in range(5):
            line = f"line {number}\n".encode()
            stream.stdin.write(line)
            stream.stdin.flush()
            assert _next_within(stream) == ("stdout", line)
        assert waits.timeouts(owned) == []
    finally:
        stream.close()
    _assert_process_released(stream)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_deadline_stream_waits_for_the_remaining_time_in_one_wait(
    waits: WaitLog,
) -> None:
    before = set(threading.enumerate())
    stream = stream_stdout_and_stderr(
        [sys.executable, "-c", "input()"], pipe_stdin=True, timeout=30
    )
    owned = {thread.ident for thread in set(threading.enumerate()) - before}
    try:
        sleep(0.3)

        (timeout,) = waits.timeouts(owned)
        assert 29 < timeout <= 30
    finally:
        stream.close()
    _assert_process_released(stream)


_HOLDER = (
    "import os, signal, sys\n"
    "release, status = map(int, sys.argv[1:])\n"
    "if os.fork() == 0:\n"
    "    os.write(status, b'R')\n"
    "    os.read(release, 1)\n"
    "    os._exit(0)\n"
    "os.close(release)\n"
    "os.close(status)\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
@pytest.mark.parametrize("ending", ["close", "expiry", "failure"])
def test_process_stream_ending_stops_workers_while_descendant_holds_output(
    ending: Literal["close", "expiry", "failure"],
) -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    with (
        os.fdopen(release_read, "rb", buffering=0) as release_reader,
        os.fdopen(release_write, "wb", buffering=0) as release_writer,
        os.fdopen(status_read, "rb", buffering=0) as status_reader,
        os.fdopen(status_write, "wb", buffering=0) as status_writer,
    ):
        # Given a child whose descendant keeps stdout and stderr open after the
        # child itself is stopped.
        stream = stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                _HOLDER
                + ("os.write(1, b'oversize\\n')\n" if ending == "failure" else "")
                + "signal.pause()\n",
                release_reader.fileno(),
                status_writer.fileno(),
            ],
            pass_fds=(release_reader.fileno(), status_writer.fileno()),
            timeout=1 if ending == "expiry" else None,
            buffer_bytes=3 if ending == "failure" else None,
        )
        try:
            release_reader.close()
            status_writer.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b"R"

            # When the stream is closed, expires, or fails.
            match ending:
                case "close":
                    closer = Thread(target=stream.close, daemon=True)
                    closer.start()
                    closer.join(timeout=3)
                    assert not closer.is_alive(), "close waited for the descendant"
                case "expiry" | "failure":
                    assert stream._released.wait(timeout=3)
                case unreachable:
                    assert_never(unreachable)

            # Then every worker stopped, the child was reaped, and the error
            # is delivered once while the descendant still holds the pipes.
            _assert_process_released(stream)
            assert not select.select([status_reader], [], [], 0)[0]
            match ending:
                case "close":
                    assert list(stream) == []
                case "expiry":
                    with pytest.raises(DockerTimeout):
                        next(stream)
                case "failure":
                    with pytest.raises(BufferError):
                        next(stream)
                case unreachable:
                    assert_never(unreachable)
            assert list(stream) == []
        finally:
            release_writer.close()
            stream.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b""


@pytest.mark.parametrize("fail_at", [2, 3, 4])
@pytest.mark.parametrize("buffer_bytes", [None, 10])
def test_process_stream_setup_failure_unwinds_child_pipes_and_threads(
    fail_at: int, buffer_bytes: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[subprocess.Popen[bytes]] = []
    starts = 0
    saturated = Event()
    rescued = Event()
    queues: list[StreamBuffer] = []
    put = StreamBuffer.put

    def enqueue(queue: StreamBuffer, item: tuple[str, bytes]) -> bool:
        with queue.condition:
            if buffer_bytes is not None and queue._bytes + len(item[1]) > buffer_bytes:
                queues.append(queue)
                saturated.set()
            return put(queue, item)

    def rescue() -> None:
        rescued.set()
        queues[0].close()

    rescue_timer = threading.Timer(3, rescue)

    class FailingThread(Thread):
        @override
        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == fail_at:
                if buffer_bytes is not None:
                    assert saturated.wait(timeout=3)
                    rescue_timer.start()
                raise RuntimeError("can't start new thread")
            super().start()

    def launch(
        args: list[str],
        *,
        stdin: int | None,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            args, stdin=stdin, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds
        )
        launched.append(process)
        return process

    monkeypatch.setattr(pywhales._process_stream, "Thread", FailingThread)
    monkeypatch.setattr(pywhales.utils, "Popen", launch)
    monkeypatch.setattr(StreamBuffer, "put", enqueue)
    before = set(threading.enumerate())

    try:
        with pytest.raises(RuntimeError):
            stream_stdout_and_stderr(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x\\n' * 100); input()",
                ],
                pipe_stdin=True,
                buffer_bytes=buffer_bytes,
            )
    finally:
        rescue_timer.cancel()
        if rescue_timer.ident is not None:
            rescue_timer.join()

    assert not rescued.is_set(), "setup cleanup waited for queue capacity"
    (process,) = launched
    assert process.returncode is not None
    assert all(
        pipe.closed for pipe in (process.stdin, process.stdout, process.stderr) if pipe
    )
    assert set(threading.enumerate()) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
@pytest.mark.parametrize("failed_body", [False, True])
def test_stdin_pump_full_pipe_waits_without_timers_and_cancels_with_exact_counters(
    failed_body: bool, monkeypatch: pytest.MonkeyPatch, waits: WaitLog
) -> None:
    # Given a child that never reads stdin, whose descendant keeps the pipe open,
    # and more input than the pipe holds.
    blocked: Counter[int] = Counter()
    accepted: Counter[int] = Counter()
    write = os.write

    def counted(fd: int, data: Buffer) -> int:
        try:
            count = write(fd, data)
        except BlockingIOError:
            blocked[fd] += 1
            raise
        accepted[fd] += count
        return count

    monkeypatch.setattr(os, "write", counted)
    payload = b"x" * (8 * 1024 * 1024)
    telemetry = ExecutionTelemetry(None, "captured")
    release_read, release_write = os.pipe()
    code = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.read(int(sys.argv[1]), 1)\n"
        "    os._exit(0)\n"
        "time.sleep(60)\n"
    )
    try:
        with subprocess.Popen(
            [sys.executable, "-c", code, str(release_read)],
            stdin=subprocess.PIPE,
            pass_fds=(release_read,),
        ) as child:
            try:
                assert child.stdin is not None
                fd = child.stdin.fileno()
                before = set(threading.enumerate())
                pump = StdinPump(child, payload, telemetry)
                pump.__enter__()
                owned = {thread.ident for thread in set(threading.enumerate()) - before}
                deadline = monotonic() + 3
                while not blocked[fd] and monotonic() < deadline:
                    sleep(0.01)

                # When the writer sits on a full pipe for a while.
                sleep(0.3)

                # Then it neither retried on a timer nor requested one.
                assert 0 < blocked[fd] <= 2
                assert waits.timeouts(owned) == []

                # And leaving the context stops it promptly, with exact counts,
                # although a descendant still holds the pipe open.
                error = RuntimeError("body failed") if failed_body else None
                leaving = Thread(
                    target=pump.__exit__,
                    args=(RuntimeError if failed_body else None, error, None),
                    daemon=True,
                )
                leaving.start()
                leaving.join(timeout=3)
                assert not leaving.is_alive(), "leaving waited for the descendant"
                assert pump._thread is not None and not pump._thread.is_alive()
                assert telemetry.stdin_bytes == accepted[fd]
                assert 0 < accepted[fd] < len(payload)
                assert pump._pipe is not None and pump._pipe.closed
            finally:
                child.kill()
    finally:
        os.write(release_write, b"x")
        os.close(release_read)
        os.close(release_write)


def test_stdin_pump_start_failure_kills_child_and_keeps_stdin_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingThread(Thread):
        @override
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(pywhales._stdin_pump, "Thread", FailingThread)
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE
    ) as child:
        try:
            with (
                pytest.raises(RuntimeError),
                StdinPump(child, b"data", ExecutionTelemetry(None, "captured")),
            ):
                pytest.fail("entered with a writer that could not start")

            assert child.wait(timeout=3) == -signal.SIGKILL
            assert child.stdin is not None
        finally:
            child.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wake descriptors")
def test_wake_latch_wakes_pollers_once_and_closes_idempotently() -> None:
    latch = WakeLatch()
    fd = latch.fileno()
    assert fd is not None
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    assert poller.poll(0) == [] and not latch.is_set()

    latch.set()
    latch.set()

    assert latch.is_set()
    assert poller.poll(0) and poller.poll(0)
    latch.close()
    latch.close()
    latch.set()
    assert latch.fileno() is None and latch.is_set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX wake descriptors")
def test_wake_latch_set_and_close_race_without_errors() -> None:
    errors: list[BaseException] = []

    def attempt(action: Callable[[], None]) -> None:
        try:
            action()
        except (OSError, TypeError, ValueError, AttributeError) as error:
            errors.append(error)

    for _ in range(300):
        latch = WakeLatch()
        workers = [
            Thread(target=attempt, args=(action,))
            for action in (latch.set, latch.close, latch.set, latch.close)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        latch.close()

    assert errors == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_wait_ready_blocks_until_the_latch_is_set() -> None:
    read_fd, write_fd = os.pipe()
    latch = WakeLatch()
    finished = Event()
    try:
        waiter = Thread(
            target=lambda: (wait_ready(read_fd, latch=latch), finished.set()),
            daemon=True,
        )
        waiter.start()
        assert not finished.wait(0.3)

        latch.set()

        assert finished.wait(3)
        waiter.join(timeout=3)
    finally:
        latch.set()
        latch.close()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_wait_ready_honours_timeouts_and_survives_huge_ones() -> None:
    read_fd, write_fd = os.pipe()
    try:
        started = monotonic()
        wait_ready(read_fd, timeout=0.2)
        assert 0.15 <= monotonic() - started < 3

        os.write(write_fd, b"x")
        wait_ready(read_fd, timeout=1e308)
        wait_ready(write_fd, writable=True, timeout=float("inf"))
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_windows_waits_stay_bounded_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    naps: list[float] = []
    latch = WakeLatch()
    monkeypatch.setattr(pywhales._readiness, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(pywhales._readiness.time, "sleep", naps.append)
    try:
        wait_ready(0)
        wait_ready(0, timeout=0.001)
        wait_ready(0, latch=latch)
        latch.set()
        started = monotonic()
        wait_ready(0, latch=latch)

        assert naps == [pywhales._readiness.POLL_SLICE, 0.001]
        assert monotonic() - started < 3
    finally:
        latch.close()


_SCOPE_BLOCKER = (
    "import os,threading; os.write(1,b'ready\\n'); threading.Event().wait()"
)


def _scope_policy(argv: int = 65536) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=argv, max_response_bytes=1024
    )


def _scoped_stream(
    code: str = _SCOPE_BLOCKER,
    *,
    deadline: float | None = None,
    cancel: Event | None = None,
    timeout: float | None = None,
) -> ProcessStream:
    """Launch a stream inside a scope that has already exited when this returns."""
    with observation(_scope_policy(), deadline=deadline, cancel=cancel):
        return stream_stdout_and_stderr([sys.executable, "-c", code], timeout=timeout)


def _forbid_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError("a process was launched")

    monkeypatch.setattr(pywhales.utils, "Popen", launch)


def test_scoped_stream_never_launches_for_a_cancelled_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_launch(monkeypatch)
    cancel = Event()
    with (
        pytest.raises(ObservationCancelled),
        observation(_scope_policy(), cancel=cancel),
    ):
        cancel.set()
        stream_stdout_and_stderr([sys.executable, "-c", "pass"])


def test_scoped_stream_never_launches_for_an_expired_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_launch(monkeypatch)
    expired = ObservationState(
        policy=_scope_policy(),
        deadline=monotonic() - 1,
        cancel_tokens=(),
        response_accounts=(),
    )

    def launch() -> ProcessStream:
        _current_observation.set(expired)
        return stream_stdout_and_stderr([sys.executable, "-c", "pass"])

    with pytest.raises(ObservationTimeout):
        Context().run(launch)


def test_scoped_stream_checks_argv_and_environment_limits_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_launch(monkeypatch)
    command = [sys.executable, "-c", "pass"]
    with (
        observation(_scope_policy(argv=8)),
        pytest.raises(ObservationLimitError) as oversized,
    ):
        stream_stdout_and_stderr(command)
    assert (oversized.value.reason, oversized.value.limit) == ("argv", 8)

    invalid = {"PRIVATE": "secret\0value"}
    with observation(_scope_policy()), pytest.raises(ObservationLimitError) as unsafe:
        stream_stdout_and_stderr(command, env=invalid)
    assert unsafe.value.reason == "environment"
    assert "secret" not in "".join(traceback.format_exception(unsafe.value))

    with pytest.raises(ValueError):
        stream_stdout_and_stderr(command, env=invalid)


def test_scoped_stream_translates_native_argv_refusal_only_when_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError(errno.E2BIG, "secret")

    monkeypatch.setattr(pywhales.utils, "Popen", refuse)
    command = [sys.executable, "-c", "pass"]
    with (
        observation(_scope_policy()),
        pytest.raises(ObservationLimitError) as scoped,
    ):
        stream_stdout_and_stderr(command)
    assert scoped.value.reason == "native_argv"
    assert scoped.value.__cause__ is None

    with pytest.raises(OSError) as unscoped:
        stream_stdout_and_stderr(command)
    assert not isinstance(unscoped.value, ObservationLimitError)


def test_scope_deadline_reaps_the_child_and_raises_observation_timeout() -> None:
    stream = _scoped_stream(deadline=monotonic() + 0.4)
    try:
        assert next(stream) == ("stdout", b"ready\n")

        # The watchdog stops the child although nothing iterates the stream.
        assert stream._released.wait(timeout=3)
        _assert_process_released(stream)
        assert stream._process.returncode == -signal.SIGKILL
        with pytest.raises(ObservationTimeout):
            next(stream)
        assert list(stream) == []
    finally:
        stream.close()


def test_cancel_token_reaps_the_child_after_the_scope_exited() -> None:
    cancel = Event()
    stream = _scoped_stream(cancel=cancel)
    try:
        assert next(stream) == ("stdout", b"ready\n")

        # The launch-time token still applies once the with block has ended.
        cancel.set()
        assert stream._released.wait(timeout=3)
        _assert_process_released(stream)
        with pytest.raises(ObservationCancelled):
            next(stream)
        assert list(stream) == []
    finally:
        stream.close()


def test_cancellation_is_delivered_once_through_close_and_context_exit() -> None:
    cancel = Event()
    with _scoped_stream(cancel=cancel) as stream:
        assert next(stream) == ("stdout", b"ready\n")
        cancel.set()
        stream.close()
        stream.close()
        assert stream._released.wait(timeout=3)
        _assert_process_released(stream)
        with pytest.raises(ObservationCancelled):
            next(stream)
    assert list(stream) == []


def test_scope_error_is_raised_by_clean_context_exit() -> None:
    cancel = Event()
    stream = _scoped_stream(cancel=cancel)
    with pytest.raises(ObservationCancelled), stream:
        assert next(stream) == ("stdout", b"ready\n")
        cancel.set()
        assert stream._released.wait(timeout=3)
    _assert_process_released(stream)


def test_scope_error_does_not_replace_a_body_exception() -> None:
    cancel = Event()
    stream = _scoped_stream(cancel=cancel)
    with pytest.raises(RuntimeError), stream:
        cancel.set()
        assert stream._released.wait(timeout=3)
        raise RuntimeError("body")
    _assert_process_released(stream)


def test_cancellation_after_natural_exit_discards_undelivered_output() -> None:
    cancel = Event()
    stream = _scoped_stream("print('pending')", cancel=cancel)
    try:
        assert stream._released.wait(timeout=3)
        assert stream._process.returncode == 0

        cancel.set()

        with pytest.raises(ObservationCancelled):
            next(stream)
        assert list(stream) == []
    finally:
        stream.close()


def test_natural_completion_inside_a_live_scope_is_unchanged() -> None:
    cancel = Event()
    with observation(_scope_policy(), deadline=monotonic() + 30, cancel=cancel):
        stream = stream_stdout_and_stderr([sys.executable, "-c", "print('done')"])
        assert list(stream) == [("stdout", b"done\n")]
    stream.close()
    _assert_process_released(stream)


@pytest.mark.parametrize("selected_first", [True, False])
def test_command_deadline_precedes_only_a_later_scope_deadline(
    selected_first: bool,
) -> None:
    expected = DockerTimeout if selected_first else ObservationTimeout
    stream = _scoped_stream(
        deadline=monotonic() + (30 if selected_first else 0.4),
        timeout=0.4 if selected_first else 30,
    )
    try:
        assert next(stream) == ("stdout", b"ready\n")
        with pytest.raises(expected):
            next(stream)
        _assert_process_released(stream)
    finally:
        stream.close()


def test_equal_command_and_scope_deadlines_report_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The launch deadline is computed from a pinned clock, so both are the same
    # float; the strictly-earlier rule of captured runs gives the tie to the scope.
    launch_clock = monotonic()
    monkeypatch.setattr(pywhales.utils, "monotonic", lambda: launch_clock)
    stream = _scoped_stream(deadline=launch_clock + 0.5, timeout=0.5)
    try:
        assert stream._deadline == launch_clock + 0.5
        with pytest.raises(ObservationTimeout):
            list(stream)
        _assert_process_released(stream)
    finally:
        stream.close()


def test_cancel_token_wins_over_command_and_scope_deadlines() -> None:
    cancel = Event()
    stream = _scoped_stream(deadline=monotonic() + 30, cancel=cancel, timeout=30)
    try:
        assert next(stream) == ("stdout", b"ready\n")
        # Make every stop reason true at once; close() evaluates them together.
        stream._deadline = monotonic() - 1
        cancel.set()

        stream.close()

        with pytest.raises(ObservationCancelled):
            next(stream)
        _assert_process_released(stream)
    finally:
        stream.close()


def test_unscoped_stream_ignores_a_later_scope_and_keeps_command_timeouts() -> None:
    stream = stream_stdout_and_stderr([sys.executable, "-c", "print('free')"])
    assert stream._observation is None
    cancel = Event()
    with (
        pytest.raises(ObservationCancelled),
        observation(_scope_policy(), cancel=cancel),
    ):
        cancel.set()
        assert list(stream) == [("stdout", b"free\n")]
    _assert_process_released(stream)

    bounded = stream_stdout_and_stderr(
        [sys.executable, "-c", _SCOPE_BLOCKER], timeout=0.2
    )
    try:
        assert next(bounded) == ("stdout", b"ready\n")
        with pytest.raises(DockerTimeout):
            next(bounded)
    finally:
        bounded.close()


def test_scope_without_deadline_or_token_adds_no_monitoring() -> None:
    with observation(_scope_policy()):
        stream = stream_stdout_and_stderr(
            [sys.executable, "-c", "input()"], pipe_stdin=True
        )
    try:
        assert stream._observation is None
        assert stream._wake_in() is None
    finally:
        stream.close()
    _assert_process_released(stream)


def test_early_close_of_scoped_stream_closes_pipes_and_joins_workers() -> None:
    cancel = Event()
    for consume in (False, True):
        stream = _scoped_stream(cancel=cancel)
        if consume:
            assert next(stream) == ("stdout", b"ready\n")
        stream.close()
        stream.close()
        assert list(stream) == []
        _assert_process_released(stream)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork and inherited fds")
def test_scope_deadline_stops_a_stream_whose_descendant_holds_the_pipes() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    with (
        os.fdopen(release_read, "rb", buffering=0) as release_reader,
        os.fdopen(release_write, "wb", buffering=0) as release_writer,
        os.fdopen(status_read, "rb", buffering=0) as status_reader,
        os.fdopen(status_write, "wb", buffering=0) as status_writer,
    ):
        # Given a child that exits while its descendant keeps both pipes open.
        with observation(_scope_policy(), deadline=monotonic() + 0.6):
            stream = stream_stdout_and_stderr(
                [
                    sys.executable,
                    "-c",
                    _HOLDER,
                    str(release_reader.fileno()),
                    str(status_writer.fileno()),
                ],
                pass_fds=(release_reader.fileno(), status_writer.fileno()),
            )
        try:
            release_reader.close()
            status_writer.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b"R"

            # When the scope deadline passes while the readers still wait.
            assert stream._released.wait(timeout=3)

            # Then the readers were stopped and the error is delivered once.
            _assert_process_released(stream)
            with pytest.raises(ObservationTimeout):
                next(stream)
            assert list(stream) == []
        finally:
            release_writer.close()
            stream.close()
            assert select.select([status_reader], [], [], 3)[0]
            assert status_reader.read(1) == b""


@dataclass
class StreamWaits:
    """Timers requested by worker-thread condition waits, and the first request."""

    timeouts: list[float] = field(default_factory=list)
    first: Event = field(default_factory=Event)


@pytest.fixture
def stream_waits(monkeypatch: pytest.MonkeyPatch) -> StreamWaits:
    """Record every timed condition wait made off the main thread."""
    log = StreamWaits()
    wait = threading.Condition.wait

    def record(self: threading.Condition, timeout: float | None = None) -> bool:
        if timeout is not None and threading.current_thread() is not (
            threading.main_thread()
        ):
            log.timeouts.append(timeout)
            log.first.set()
        return wait(self, timeout)

    monkeypatch.setattr(threading.Condition, "wait", record)
    return log


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_scoped_stream_waits_use_only_the_bounded_cancel_check(
    stream_waits: StreamWaits,
) -> None:
    cancel = Event()
    stream = _scoped_stream(cancel=cancel)
    try:
        assert stream_waits.first.wait(timeout=3)
        cancel.set()
        assert stream._released.wait(timeout=3)
    finally:
        stream.close()
    assert all(0 <= timeout <= CANCEL_CHECK for timeout in stream_waits.timeouts)
    _assert_process_released(stream)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readiness waits")
def test_scoped_stream_deadline_wait_is_one_wait_for_the_remaining_time(
    stream_waits: StreamWaits,
) -> None:
    stream = _scoped_stream(deadline=monotonic() + 30)
    try:
        assert stream_waits.first.wait(timeout=3)
        (timeout,) = stream_waits.timeouts
        assert 29 < timeout <= 30
    finally:
        stream.close()
    _assert_process_released(stream)
