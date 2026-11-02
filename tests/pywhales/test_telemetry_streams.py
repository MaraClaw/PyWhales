import errno
import json
import sys
from dataclasses import asdict, replace
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch

import pytest

from pywhales._observation import observation
from pywhales.client_config import ClientConfig
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import (
    CompletionRecord,
    LaunchRecord,
    RefusalRecord,
    TelemetryRecord,
)
from pywhales.utils import MappedProcessStream, stream_stdout_and_stderr


def command(records: list[TelemetryRecord], code: str) -> list[object]:
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    return [*config.docker_cmd, "-c", code]


def completion(records: list[TelemetryRecord]) -> CompletionRecord:
    assert isinstance(records[0], LaunchRecord)
    assert records[0].family == "stream"
    assert len(records) == 2
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert records[0].schema_version == completed.schema_version == 6
    assert completed.attempt_id == records[0].attempt_id
    assert completed.stdin_bytes is None
    return completed


def test_stream_completes_on_exhaustion_not_when_child_exits() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "import os; os.write(1,b'\\xff\\n'); os.write(2,b'err')")
    )
    try:
        assert stream._released.wait(timeout=3)
        assert len(records) == 1 and isinstance(records[0], LaunchRecord)

        output = list(stream)

        assert sorted(output) == [("stderr", b"err"), ("stdout", b"\xff\n")]
        completed = completion(records)
        assert completed.return_code == stream._process.returncode == 0
        assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
            "success",
            2,
            3,
        )
    finally:
        stream.close()
    assert completion(records) is completed


@pytest.mark.parametrize("consume", [False, True])
def test_explicit_close_completes_idle_stream_once(consume: bool) -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "print('ready',flush=True); input()"), pipe_stdin=True
    )
    try:
        if consume:
            assert next(stream) == ("stdout", b"ready\n")

        stream.close()
        stream.close()

        assert list(stream) == []
        assert completion(records).outcome == "closed"
        assert completion(records).return_code == stream._process.returncode
        assert stream._process.poll() is not None
        assert not stream._watchdog.is_alive()
        assert all(not thread.is_alive() for thread in stream._threads)
    finally:
        stream.close()


def test_natural_nonzero_exit_reports_failure_after_drain() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "print('output'); raise SystemExit(7)")
    )

    with pytest.raises(DockerException) as raised:
        list(stream)
    stream.close()

    assert raised.value.return_code == 7
    assert completion(records).outcome == "failure"
    assert completion(records).return_code == stream._process.returncode == 7


def test_close_keeps_recorded_timeout_outcome_and_exception() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "input()"), pipe_stdin=True, timeout=0.1
    )
    assert stream._released.wait(timeout=3)

    stream.close()

    with pytest.raises(DockerTimeout):
        next(stream)
    assert completion(records).outcome == "timeout"
    assert completion(records).return_code == stream._process.returncode
    assert list(stream) == []


def test_close_keeps_recorded_buffer_failure_and_measured_read_count() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "print('oversized',flush=True); input()"),
        pipe_stdin=True,
        buffer_bytes=3,
    )
    assert stream._released.wait(timeout=3)

    stream.close()

    with pytest.raises(BufferError):
        next(stream)
    completed = completion(records)
    assert (completed.outcome, completed.stdout_bytes) == ("failure", 10)
    assert completed.return_code == stream._process.returncode


def test_throwing_observer_never_changes_original_stream_failure_or_confidentiality() -> (
    None
):
    records: list[TelemetryRecord] = []
    secret = "private-stream-sentinel"

    def throwing(record: TelemetryRecord) -> None:
        records.append(record)
        raise RuntimeError(secret)

    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=throwing, raw_diagnostics=True
    )
    stream = stream_stdout_and_stderr(
        [*config.docker_cmd, "-c", f"print({secret!r}); raise SystemExit(7)"],
        env={"PRIVATE": secret},
    )

    with pytest.raises(DockerException) as raised, stream:
        assert next(stream) == ("stdout", (secret + "\n").encode())
        next(stream)

    assert raised.value.return_code == 7
    assert completion(records).outcome == "failure"
    assert completion(records).return_code == stream._process.returncode == 7
    assert secret not in json.dumps([asdict(record) for record in records])


def test_mapper_failure_and_body_failure_are_preserved_when_stream_closes() -> None:
    records: list[TelemetryRecord] = []
    child = stream_stdout_and_stderr(
        command(records, "print('ready',flush=True); input()"), pipe_stdin=True
    )
    original = ValueError("consumer-private")

    def fail(record: tuple[str, bytes]) -> bytes:
        raise original

    mapped = MappedProcessStream(child, map(fail, child))

    with pytest.raises(ValueError) as raised, mapped:
        next(mapped)

    assert raised.value is original
    assert completion(records).outcome == "closed"


def test_concurrent_close_emits_one_completion() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(command(records, "input()"), pipe_stdin=True)
    closers = [Thread(target=stream.close) for _ in range(4)]

    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(timeout=3)
        assert not closer.is_alive()

    assert completion(records).outcome == "closed"
    assert completion(records).return_code == stream._process.returncode
    assert stream._process.returncode is not None


def test_close_and_read_race_after_watchdog_timeout_delivers_error_once() -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(
        command(records, "input()"), pipe_stdin=True, timeout=0.1
    )
    assert stream._released.wait(timeout=3)
    ready = Event()
    errors: list[DockerTimeout] = []

    def consume() -> None:
        ready.wait()
        try:
            next(stream)
        except DockerTimeout as error:
            errors.append(error)

    def close() -> None:
        ready.wait()
        stream.close()

    workers = [Thread(target=consume), Thread(target=close)]
    for worker in workers:
        worker.start()
    ready.set()
    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert len(errors) == 1
    assert list(stream) == []
    assert completion(records).outcome == "timeout"
    assert completion(records).return_code == stream._process.returncode


def test_overlapping_streams_correlate_reverse_completion_with_reaped_status() -> None:
    records: list[TelemetryRecord] = []
    first = stream_stdout_and_stderr(
        command(records, "input(); raise SystemExit(7)"), pipe_stdin=True
    )
    second = stream_stdout_and_stderr(
        command(records, "input(); raise SystemExit(3)"), pipe_stdin=True
    )
    try:
        assert first.stdin is not None and second.stdin is not None
        second.stdin.write(b"go\n")
        second.stdin.flush()
        with pytest.raises(DockerException):
            list(second)
        first.stdin.write(b"go\n")
        first.stdin.flush()
        with pytest.raises(DockerException):
            list(first)
        launches = [record for record in records if isinstance(record, LaunchRecord)]
        completed = [
            record for record in records if isinstance(record, CompletionRecord)
        ]
        assert len(launches) == len(completed) == 2
        assert launches[0].attempt_id != launches[1].attempt_id
        assert [record.attempt_id for record in completed] == [
            launches[1].attempt_id,
            launches[0].attempt_id,
        ]
        assert (
            [record.return_code for record in completed]
            == [
                second._process.returncode,
                first._process.returncode,
            ]
            == [3, 7]
        )
    finally:
        first.close()
        second.close()


def test_close_wins_pending_nonzero_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(command(records, "raise SystemExit(7)"))
    reaped, resume = Event(), Event()
    original_raise = stream._raise_error
    errors: list[DockerException] = []

    def paused() -> None:
        original_raise()
        reaped.set()
        assert resume.wait(timeout=3)

    def consume() -> None:
        try:
            list(stream)
        except DockerException as error:
            errors.append(error)

    monkeypatch.setattr(stream, "_raise_error", paused)
    consumer = Thread(target=consume)
    consumer.start()
    try:
        assert reaped.wait(timeout=3)
        stream.close()
        resume.set()
        consumer.join(timeout=3)
        assert not consumer.is_alive()
        assert errors == []
        assert completion(records).outcome == "closed"
        assert completion(records).return_code == stream._process.returncode == 7
    finally:
        resume.set()
        consumer.join(timeout=3)
        stream.close()


def test_stream_spawn_failure_preserves_original_error() -> None:
    records: list[TelemetryRecord] = []
    original = OSError("private-executable")
    with (
        patch("pywhales.utils.Popen", side_effect=original),
        pytest.raises(OSError) as raised,
    ):
        stream_stdout_and_stderr(command(records, "pass"))

    assert raised.value is original
    completed = completion(records)
    assert completed.return_code is None
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "failure",
        None,
        None,
    )


def test_policy_snapshot_survives_mutation_during_stream_lifetime() -> None:
    original: list[TelemetryRecord] = []
    fresh: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=original.append
    )
    stream = stream_stdout_and_stderr([*config.docker_cmd, "-c", "pass"])
    config.telemetry_observer = fresh.append

    with stream:
        assert list(stream) == []

    assert completion(original).outcome == "success"
    assert fresh == []


def scope_policy(argv: int = 65536) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=argv, max_response_bytes=1024
    )


def test_scoped_launch_rejection_emits_refusals() -> None:
    records: list[TelemetryRecord] = []
    cancel = Event()
    with (
        pytest.raises(ObservationCancelled),
        observation(scope_policy(), cancel=cancel),
    ):
        cancel.set()
        stream_stdout_and_stderr(command(records, "pass"))
    with (
        observation(scope_policy(argv=8)),
        pytest.raises(ObservationLimitError),
    ):
        stream_stdout_and_stderr(command(records, "pass"))

    assert len(records) == 2
    assert all(isinstance(record, RefusalRecord) for record in records)
    assert [
        record.outcome for record in records if isinstance(record, RefusalRecord)
    ] == [
        "cancelled",
        "limit",
    ]


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_scope_stop_finishes_telemetry_once_after_cleanup(stop: str) -> None:
    records: list[TelemetryRecord] = []
    cancel = Event()
    with observation(
        scope_policy(),
        deadline=monotonic() + 0.4 if stop == "deadline" else None,
        cancel=cancel,
    ):
        stream = stream_stdout_and_stderr(
            command(records, "print('ready',flush=True); input()"), pipe_stdin=True
        )
    try:
        assert next(stream) == ("stdout", b"ready\n")
        if stop == "cancel":
            cancel.set()
        assert stream._released.wait(timeout=3)
        assert len(records) == 1

        with pytest.raises(
            ObservationTimeout if stop == "deadline" else ObservationCancelled
        ):
            next(stream)
        stream.close()
        stream.close()

        completed = completion(records)
        assert completed.outcome == ("timeout" if stop == "deadline" else "cancelled")
        assert completed.return_code == stream._process.returncode
        assert completed.stdout_bytes == len(b"ready\n")
        assert list(stream) == []
        assert completion(records) is completed
    finally:
        stream.close()


def test_close_before_a_scope_stop_finishes_telemetry_as_closed() -> None:
    records: list[TelemetryRecord] = []
    cancel = Event()
    with observation(scope_policy(), cancel=cancel):
        stream = stream_stdout_and_stderr(command(records, "input()"), pipe_stdin=True)

    stream.close()
    cancel.set()
    stream.close()

    assert list(stream) == []
    assert completion(records).outcome == "closed"


def test_close_racing_a_set_cancel_token_reports_cancelled() -> None:
    records: list[TelemetryRecord] = []
    cancel = Event()
    with observation(scope_policy(), cancel=cancel):
        stream = stream_stdout_and_stderr(command(records, "input()"), pipe_stdin=True)

    cancel.set()
    stream.close()

    with pytest.raises(ObservationCancelled):
        next(stream)
    completed = completion(records)
    assert completed.outcome == "cancelled"
    assert completed.return_code == stream._process.returncode
    assert list(stream) == []
    assert completion(records) is completed


def test_stream_budget_overflow_finishes_telemetry_once_as_limit() -> None:
    records: list[TelemetryRecord] = []
    policy = replace(scope_policy(), max_total_stream_bytes=4)
    with observation(policy):
        stream = stream_stdout_and_stderr(
            command(
                records,
                "import os,threading; os.write(1,b'abcdefgh'); threading.Event().wait()",
            )
        )
    try:
        assert stream._released.wait(timeout=3)
        assert len(records) == 1

        with pytest.raises(ObservationLimitError) as failure:
            next(stream)
        stream.close()

        assert failure.value.reason == "total_stream"
        completed = completion(records)
        assert completed.outcome == "limit"
        assert completed.return_code == stream._process.returncode
        assert (completed.stdout_bytes, completed.stderr_bytes) == (5, 0)
        assert list(stream) == []
        assert completion(records) is completed
    finally:
        stream.close()


def test_close_after_stream_overflow_keeps_the_limit_outcome() -> None:
    records: list[TelemetryRecord] = []
    policy = replace(scope_policy(), max_total_stream_bytes=4)
    with observation(policy):
        stream = stream_stdout_and_stderr(
            command(
                records,
                "import os,threading; os.write(2,b'abcdefgh'); threading.Event().wait()",
            )
        )
    assert stream._released.wait(timeout=3)

    stream.close()

    assert completion(records).outcome == "limit"
    with pytest.raises(ObservationLimitError):
        next(stream)
    assert completion(records).outcome == "limit"


def test_stream_budget_cut_short_by_a_deadline_keeps_the_earlier_stop() -> None:
    records: list[TelemetryRecord] = []
    policy = replace(scope_policy(), max_total_stream_bytes=4)
    with observation(policy, deadline=monotonic() + 0.3):
        stream = stream_stdout_and_stderr(command(records, "input()"), pipe_stdin=True)
    with stream, pytest.raises(ObservationTimeout):
        list(stream)

    assert completion(records).outcome == "timeout"


@pytest.mark.parametrize(
    ("expired", "scope", "reader", "delivered", "outcome"),
    [
        (True, ObservationCancelled(), OSError(), DockerTimeout, "timeout"),
        (
            False,
            ObservationLimitError("total_stream", 1, 2),
            OSError(),
            ObservationLimitError,
            "limit",
        ),
        (
            False,
            ObservationCancelled(),
            BufferError(),
            ObservationCancelled,
            "cancelled",
        ),
        (False, ObservationTimeout(), OSError(), ObservationTimeout, "timeout"),
        (False, None, BufferError(), BufferError, "failure"),
        (False, None, OSError(), OSError, "failure"),
    ],
)
def test_stream_outcome_matches_the_error_it_delivers(
    expired: bool,
    scope: ObservationCancelled | ObservationTimeout | ObservationLimitError | None,
    reader: OSError | BufferError,
    delivered: type[BaseException],
    outcome: str,
) -> None:
    # Given every stop recorded at once, which no real race can arrange on demand.
    records: list[TelemetryRecord] = []
    stream = stream_stdout_and_stderr(command(records, "input()"), pipe_stdin=True)
    try:
        with stream._queue.condition:
            if expired:
                stream._expired = True
                stream._timeout_stdout = b""
            stream._error = reader
            if scope is not None:
                stream._scope_error = scope
            stream._stop.set()
            stream._queue.close()

        # When the stream delivers its terminal error.
        with pytest.raises(delivered):
            next(stream)

        # Then the completion reports the delivered error's outcome, once.
        assert completion(records).outcome == outcome
    finally:
        stream.close()


def test_native_argv_rejection_after_stream_start_reports_limit() -> None:
    records: list[TelemetryRecord] = []
    with (
        patch("pywhales.utils.Popen", side_effect=OSError(errno.E2BIG, "too big")),
        observation(scope_policy()),
        pytest.raises(ObservationLimitError) as failure,
    ):
        stream_stdout_and_stderr(command(records, "pass"))

    assert failure.value.reason == "native_argv"
    completed = completion(records)
    assert (completed.outcome, completed.return_code) == ("limit", None)


def test_other_spawn_errors_and_interrupts_stay_failures() -> None:
    for error in (OSError(errno.ENOENT, "missing"), KeyboardInterrupt()):
        records: list[TelemetryRecord] = []
        with (
            patch("pywhales.utils.Popen", side_effect=error),
            observation(scope_policy()),
            pytest.raises(type(error)),
        ):
            stream_stdout_and_stderr(command(records, "pass"))

        assert completion(records).outcome == "failure"
