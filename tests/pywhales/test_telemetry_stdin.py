import errno
import json
import os
import sys
from collections.abc import Buffer
from dataclasses import asdict
from pathlib import Path
from subprocess import Popen

import pytest

from pywhales import _stdin_pump, utils
from pywhales._observation import observation
from pywhales._stdin_pump import StdinPump
from pywhales.client_config import ClientConfig
from pywhales.exceptions import DockerTimeout, NoSuchContainer, ObservationLimitError
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import CompletionRecord, LaunchRecord, TelemetryRecord
from pywhales.telemetry._lifecycle import ExecutionTelemetry


def completion(records: list[TelemetryRecord]) -> CompletionRecord:
    assert len(records) == 2
    launch, completed = records
    assert isinstance(launch, LaunchRecord)
    assert isinstance(completed, CompletionRecord)
    assert completed.attempt_id == launch.attempt_id
    return completed


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[Popen[bytes]]:
    launched: list[Popen[bytes]] = []

    def pump(
        process: Popen[bytes], data: bytes | None, telemetry: ExecutionTelemetry
    ) -> StdinPump:
        launched.append(process)
        return StdinPump(process, data, telemetry)

    monkeypatch.setattr(utils, "StdinPump", pump)
    return launched


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_input_counts_successful_short_writes_with_concurrent_output(
    timeout: float | None, monkeypatch: pytest.MonkeyPatch, children: list[Popen[bytes]]
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    payload = b"private-input" * 10000
    written: list[int] = []
    write = os.write

    def short_write(fd: int, data: Buffer) -> int:
        count = write(fd, memoryview(data)[:137])
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", short_write)
    code = (
        "import os,sys; os.write(1,b'x'*100000); os.write(2,b'y'*100000); "
        "data=sys.stdin.buffer.read(); os.write(1,str(len(data)).encode())"
    )

    result = utils.run(
        [*config.docker_cmd, "-c", code],
        input=payload,
        return_stderr=True,
        timeout=timeout,
    )

    assert result == ("x" * 100000 + str(len(payload)), "y" * 100000)
    completed = completion(records)
    assert completed.stdin_bytes == sum(written) == len(payload)
    assert (completed.stdout_bytes, completed.stderr_bytes) == (100006, 100000)
    assert len(written) > 1 and max(written) <= 137
    assert "private-input" not in json.dumps([asdict(record) for record in records])
    assert children[0].returncode == 0
    assert all(
        pipe.closed
        for pipe in (children[0].stdin, children[0].stdout, children[0].stderr)
        if pipe is not None
    )


@pytest.mark.parametrize("payload", [None, b""])
def test_empty_measured_input_is_distinct_from_unobserved_input(
    payload: bytes | None,
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )

    assert utils.run([*config.docker_cmd, "-c", "pass"], input=payload) == ""

    assert completion(records).stdin_bytes == (None if payload is None else 0)


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_early_exit_keeps_only_accepted_input_and_original_failure(
    timeout: float | None, monkeypatch: pytest.MonkeyPatch, children: list[Popen[bytes]]
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    written: list[int] = []
    write = os.write

    def measure(fd: int, data: Buffer) -> int:
        count = write(fd, data)
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", measure)
    code = "import os; os.read(0,1); os.write(2,b'No such container: private'); os._exit(7)"
    payload = b"x" * (2 * 1024 * 1024)

    with pytest.raises(NoSuchContainer) as raised:
        utils.run([*config.docker_cmd, "-c", code], input=payload, timeout=timeout)

    completed = completion(records)
    assert completed.stdin_bytes == sum(written)
    assert 0 < sum(written) < len(payload)
    assert (completed.outcome, completed.return_code) == ("failure", 7)
    assert raised.value.stderr == "No such container: private"
    assert children[0].returncode == 7


def test_timeout_keeps_accepted_input_after_writer_and_process_cleanup(
    monkeypatch: pytest.MonkeyPatch, children: list[Popen[bytes]]
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    written: list[int] = []
    write = os.write

    def measure(fd: int, data: Buffer) -> int:
        count = write(fd, data)
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", measure)
    payload = b"x" * (2 * 1024 * 1024)
    code = "import os,threading; os.write(1,b'ready\\xff'); threading.Event().wait()"

    with pytest.raises(DockerTimeout) as raised:
        utils.run([*config.docker_cmd, "-c", code], input=payload, timeout=0.3)

    completed = completion(records)
    assert completed.stdin_bytes == sum(written)
    assert 0 < sum(written) < len(payload)
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "timeout",
        6,
        0,
    )
    assert raised.value.stdout == "ready\ufffd"
    assert children[0].returncode == completed.return_code is not None
    assert children[0].stdin is not None and children[0].stdin.closed


def test_write_failure_preserves_successful_progress_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch, children: list[Popen[bytes]]
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    write = os.write
    written: list[int] = []
    original = OSError(errno.EIO, "private-write-error")

    def fail_after_progress(fd: int, data: Buffer) -> int:
        if written:
            raise original
        count = write(fd, memoryview(data)[:17])
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", fail_after_progress)

    with pytest.raises(OSError) as raised:
        utils.run(
            [*config.docker_cmd, "-c", "import threading; threading.Event().wait()"],
            input=b"x" * 10000,
        )

    assert raised.value is original
    completed = completion(records)
    assert (completed.outcome, completed.stdin_bytes) == ("failure", 17)
    assert children[0].returncode == completed.return_code is not None


def budget(limit: int, *, response: int = 1024) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=response,
        max_total_stdin_bytes=limit,
    )


def test_observed_upload_counts_successful_short_writes_with_concurrent_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    payload = b"private-input" * 10000
    written: list[int] = []
    write = os.write

    def short_write(fd: int, data: Buffer) -> int:
        count = write(fd, memoryview(data)[:137])
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", short_write)
    code = (
        "import os,sys; os.write(1,b'x'*100000); os.write(2,b'y'*100000); "
        "data=sys.stdin.buffer.read(); os.write(1,str(len(data)).encode())"
    )

    with observation(budget(len(payload), response=1024 * 1024)) as state:
        result = utils.run(
            [*config.docker_cmd, "-c", code], input=payload, return_stderr=True
        )

    assert result == ("x" * 100000 + str(len(payload)), "y" * 100000)
    completed = completion(records)
    assert completed.outcome == "success"
    assert completed.stdin_bytes == sum(written) == len(payload)
    assert state.stdin_accounts[0].consumed == len(payload)
    assert len(written) > 1 and max(written) <= 137
    assert (completed.stdout_bytes, completed.stderr_bytes) == (100006, 100000)
    assert "private-input" not in json.dumps([asdict(record) for record in records])


def test_upload_past_the_budget_reports_limit_with_only_the_written_bytes() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    payload = b"private-input" * 200

    with (
        observation(budget(1000)) as state,
        pytest.raises(ObservationLimitError) as raised,
    ):
        utils.run(
            [*config.docker_cmd, "-c", "import sys; sys.stdin.buffer.read()"],
            input=payload,
        )

    completed = completion(records)
    assert (completed.outcome, completed.stdin_bytes) == ("limit", 1000)
    assert completed.return_code is not None
    assert state.stdin_accounts[0].consumed == 1000
    assert (raised.value.reason, raised.value.limit, raised.value.measured) == (
        "total_stdin",
        1000,
        1001,
    )
    assert "private-input" not in str(raised.value)
    assert "private-input" not in json.dumps([asdict(record) for record in records])


def test_launches_in_one_scope_share_the_remaining_budget() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    command = [*config.docker_cmd, "-c", "import sys; sys.stdin.buffer.read()"]

    with observation(budget(1000)) as state:
        utils.run(command, input=b"x" * 600)
        with pytest.raises(ObservationLimitError):
            utils.run(command, input=b"x" * 600)

    completions = [record for record in records if isinstance(record, CompletionRecord)]
    assert [(done.outcome, done.stdin_bytes) for done in completions] == [
        ("success", 600),
        ("limit", 400),
    ]
    assert state.stdin_accounts[0].consumed == 1000


def test_observed_early_exit_keeps_only_accepted_input_and_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    written: list[int] = []
    write = os.write

    def measure(fd: int, data: Buffer) -> int:
        count = write(fd, data)
        written.append(count)
        return count

    monkeypatch.setattr(_stdin_pump.os, "write", measure)
    code = "import os; os.read(0,1); os.write(2,b'No such container: private'); os._exit(7)"
    payload = b"x" * (2 * 1024 * 1024)

    with (
        observation(budget(len(payload))) as state,
        pytest.raises(NoSuchContainer) as raised,
    ):
        utils.run([*config.docker_cmd, "-c", code], input=payload)

    completed = completion(records)
    assert completed.stdin_bytes == sum(written) == state.stdin_accounts[0].consumed
    assert 0 < sum(written) < len(payload)
    assert (completed.outcome, completed.return_code) == ("failure", 7)
    assert raised.value.stderr == "No such container: private"


def test_observed_timeout_keeps_accepted_input_after_cleanup() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    payload = b"x" * (2 * 1024 * 1024)
    code = "import os,threading; os.write(1,b'ready'); threading.Event().wait()"

    with (
        observation(budget(len(payload))) as state,
        pytest.raises(DockerTimeout),
    ):
        utils.run([*config.docker_cmd, "-c", code], input=payload, timeout=0.3)

    completed = completion(records)
    assert completed.stdin_bytes == state.stdin_accounts[0].consumed
    assert 0 < state.stdin_accounts[0].consumed < len(payload)
    assert (completed.outcome, completed.stdout_bytes) == ("timeout", 5)
    assert completed.return_code is not None


def test_failed_launch_leaves_input_measurement_unknown(tmp_path: Path) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    config._client_call_with_path = [str(tmp_path / "missing-executable")]

    with pytest.raises(FileNotFoundError):
        utils.run(config.docker_cmd, input=b"not-written")

    completed = completion(records)
    assert (
        completed.stdin_bytes,
        completed.stdout_bytes,
        completed.stderr_bytes,
        completed.return_code,
    ) == (None, None, None, None)


def test_failed_observed_launch_charges_no_input(tmp_path: Path) -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    config._client_call_with_path = [str(tmp_path / "missing-executable")]

    with observation(budget(10)) as state, pytest.raises(FileNotFoundError):
        utils.run(config.docker_cmd, input=b"not-written")

    assert completion(records).stdin_bytes is None
    assert state.stdin_accounts[0].consumed == 0
