import errno
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch

import pytest

from pywhales._observation import observation
from pywhales.client_config import ClientConfig
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    NoSuchContainer,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import CompletionRecord, LaunchRecord, TelemetryRecord
from pywhales.utils import run


@pytest.mark.parametrize("timeout", [False, True])
def test_bounded_capture_deadlines_preserve_timeout_type_and_partial_bytes(
    timeout: bool,
) -> None:
    records: list[TelemetryRecord] = []
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def spawn(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdout=stdout,
            stderr=stderr,
            env=env,
            pass_fds=pass_fds,
            start_new_session=start_new_session,
        )
        children.append(process)
        return process

    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    expected = DockerTimeout if timeout else ObservationTimeout
    with (
        patch("pywhales._bounded_capture.subprocess.Popen", side_effect=spawn),
        pytest.raises(expected),
        observation(policy, deadline=None if timeout else monotonic() + 0.3),
    ):
        run(
            [
                *config.docker_cmd,
                "-c",
                "import os,threading; os.write(1,b'partial\\xff'); threading.Event().wait()",
            ],
            timeout=0.3 if timeout else None,
        )

    assert len(records) == 2
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert len(children) == 1 and children[0].returncode is not None
    assert completed.return_code == children[0].returncode
    assert children[0].stdout is not None and children[0].stdout.closed
    assert children[0].stderr is not None and children[0].stderr.closed
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "timeout",
        8,
        0,
    )


@pytest.mark.parametrize("failure", [False, True])
def test_bounded_capture_preserves_results_and_errors_when_observer_throws(
    failure: bool,
) -> None:
    records: list[TelemetryRecord] = []

    def throwing(record: TelemetryRecord) -> None:
        records.append(record)
        raise RuntimeError("private-observer-error")

    config = ClientConfig(client_call=[sys.executable], telemetry_observer=throwing)
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    args = [
        *config.docker_cmd,
        "-c",
        "import os; os.write(1,b'out\\xff\\n'); os.write(2,b'No such container: private'); "
        + ("raise SystemExit(7)" if failure else "pass"),
    ]

    with observation(policy):
        if failure:
            with pytest.raises(NoSuchContainer) as raised:
                run(args)
            assert raised.value.return_code == 7
            assert raised.value.stdout == "out\ufffd\n"
        else:
            assert run(args) == "out\ufffd"

    assert len(records) == 2
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == ("failure" if failure else "success")
    assert (completed.stdout_bytes, completed.stderr_bytes) == (5, 26)
    assert isinstance(records[0], LaunchRecord)
    assert completed.attempt_id == records[0].attempt_id
    assert completed.return_code == (7 if failure else 0)


@pytest.mark.parametrize("mode", ["deadline", "cancel", "limit"])
def test_bounded_failure_reports_reaped_native_status(mode: str) -> None:
    records: list[TelemetryRecord] = []
    children: list[subprocess.Popen[bytes]] = []
    cancel = Event()
    real_popen = subprocess.Popen

    def spawn(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdout=stdout,
            stderr=stderr,
            env=env,
            pass_fds=pass_fds,
            start_new_session=start_new_session,
        )
        children.append(process)
        if mode == "cancel":
            cancel.set()
        return process

    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=8
    )
    error = {
        "deadline": ObservationTimeout,
        "cancel": ObservationCancelled,
        "limit": ObservationLimitError,
    }[mode]
    code = "import os,threading; os.write(1,b'123456789'); threading.Event().wait()"
    with (
        patch("pywhales._bounded_capture.subprocess.Popen", side_effect=spawn),
        pytest.raises(error),
        observation(
            policy,
            deadline=monotonic() + 0.3 if mode == "deadline" else None,
            cancel=cancel,
        ),
    ):
        run(
            [
                *config.docker_cmd,
                "-c",
                code
                if mode != "deadline"
                else "import threading; threading.Event().wait()",
            ]
        )

    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout is not None and children[0].stdout.closed
    assert children[0].stderr is not None and children[0].stderr.closed
    assert len(records) == 2 and isinstance(records[0], LaunchRecord)
    assert isinstance(records[1], CompletionRecord)
    assert records[1].attempt_id == records[0].attempt_id
    assert records[1].return_code == children[0].returncode
    assert (
        records[1].outcome
        == {
            "deadline": "timeout",
            "cancel": "cancelled",
            "limit": "limit",
        }[mode]
    )


def test_bounded_native_argv_rejection_after_start_reports_limit() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )

    with (
        patch(
            "pywhales._bounded_capture.subprocess.Popen",
            side_effect=OSError(errno.E2BIG, "too big"),
        ),
        pytest.raises(ObservationLimitError) as raised,
        observation(policy),
    ):
        run([*config.docker_cmd, "-c", "pass"])

    assert raised.value.reason == "native_argv"
    assert len(records) == 2 and isinstance(records[0], LaunchRecord)
    assert isinstance(records[1], CompletionRecord)
    assert records[1].attempt_id == records[0].attempt_id
    assert (records[1].outcome, records[1].return_code) == ("limit", None)


def test_bounded_spawn_failure_pairs_records_without_status() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    failure = FileNotFoundError("private-executable")

    with (
        patch("pywhales._bounded_capture.subprocess.Popen", side_effect=failure),
        pytest.raises(FileNotFoundError) as raised,
        observation(policy),
    ):
        run([*config.docker_cmd, "-c", "pass"])

    assert raised.value is failure
    assert len(records) == 2 and isinstance(records[0], LaunchRecord)
    assert isinstance(records[1], CompletionRecord)
    assert records[1].attempt_id == records[0].attempt_id
    assert records[1].outcome == "failure" and records[1].return_code is None


@pytest.mark.parametrize("mode", ["ordinary", "deadline", "bounded"])
def test_overlapping_captures_correlate_reverse_completion(mode: str) -> None:
    records: list[TelemetryRecord] = []
    children: list[subprocess.Popen[bytes]] = []
    started = [Event(), Event()]
    finished = [Event(), Event()]
    read_first, write_first = os.pipe()
    read_second, write_second = os.pipe()
    real_popen = subprocess.Popen
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )

    def spawn(
        args: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: Mapping[str, str] | None = None,
        pass_fds: Sequence[int] = (),
        start_new_session: bool = False,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            pass_fds=pass_fds,
            start_new_session=start_new_session,
        )
        children.append(process)
        started[len(children) - 1].set()
        return process

    def execute(index: int, fd: int, status: int) -> None:
        try:
            args = [
                *config.docker_cmd,
                "-c",
                f"import os; os.read({fd},1); raise SystemExit({status})",
            ]
            if mode == "bounded":
                with observation(policy):
                    run(args, pass_fds=(fd,))
            else:
                run(args, pass_fds=(fd,), timeout=5 if mode == "deadline" else None)
        except DockerException:
            pass
        finally:
            finished[index].set()

    workers = [
        Thread(target=execute, args=(0, read_first, 7)),
        Thread(target=execute, args=(1, read_second, 3)),
    ]
    try:
        with (
            patch("pywhales.utils.Popen", side_effect=spawn),
            patch("pywhales._bounded_capture.subprocess.Popen", side_effect=spawn),
        ):
            workers[0].start()
            assert started[0].wait(3)
            workers[1].start()
            assert started[1].wait(3)
            os.write(write_second, b"x")
            assert finished[1].wait(3)
            os.write(write_first, b"x")
            assert finished[0].wait(3)
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
            == [children[1].returncode, children[0].returncode]
            == [3, 7]
        )
    finally:
        for fd in (read_first, write_first, read_second, write_second):
            os.close(fd)
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=6)
                assert not worker.is_alive()
