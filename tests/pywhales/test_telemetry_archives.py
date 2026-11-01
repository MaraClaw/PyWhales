import json
import os
import sys
import threading
from collections.abc import Buffer, Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict
from pathlib import Path
from subprocess import PIPE, Popen
from threading import Barrier
from unittest.mock import patch

import pytest

from pywhales import DockerClient, _archive_deadline
from pywhales._archive_deadline import ArchiveDeadline
from pywhales._observation import observation
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    NoSuchImage,
    ObservationLimitError,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import CompletionRecord, LaunchRecord, TelemetryRecord


def completion(records: list[TelemetryRecord]) -> CompletionRecord:
    assert len(records) == 2
    launch, completed = records
    assert isinstance(launch, LaunchRecord)
    assert isinstance(completed, CompletionRecord)
    assert launch.attempt_id == completed.attempt_id
    assert launch.family == completed.family
    return completed


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_direct_archive_load_counts_accepted_short_writes(
    timeout: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[TelemetryRecord] = []
    code = "import sys; data=sys.stdin.buffer.read(); print('Loaded image: '+str(len(data)))"
    client = DockerClient(
        client_call=[sys.executable, "-c", code],
        default_timeout=timeout,
        telemetry_observer=records.append,
    )
    payload = b"private-archive" * 100
    written: list[int] = []
    write = os.write

    def short_write(fd: int, data: Buffer) -> int:
        count = write(fd, memoryview(data)[:7])
        written.append(count)
        return count

    monkeypatch.setattr(_archive_deadline.os, "write", short_write)

    assert client.image.load(iter((payload[:17], payload[17:]))) == [str(len(payload))]

    completed = completion(records)
    assert (completed.family, completed.outcome, completed.return_code) == (
        "archive_load",
        "success",
        0,
    )
    assert completed.stdin_bytes == sum(written) == len(payload)
    assert (completed.stdout_bytes, completed.stderr_bytes) == (19, None)
    assert "private-archive" not in json.dumps([asdict(record) for record in records])


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_direct_archive_save_is_lazy_and_counts_binary_reads(
    timeout: float | None,
) -> None:
    records: list[TelemetryRecord] = []
    payload = b"\x00\xff\n" * 10000
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import os; os.write(1,b'\\x00\\xff\\n'*10000)",
        ],
        default_timeout=timeout,
        telemetry_observer=records.append,
    )
    with patch.object(client.image, "inspect", return_value=[]):
        archive = client.image.save("private-image")
    assert records == []

    assert b"".join(archive) == payload

    completed = completion(records)
    assert (completed.family, completed.outcome, completed.return_code) == (
        "archive_save",
        "success",
        0,
    )
    assert (completed.stdin_bytes, completed.stdout_bytes, completed.stderr_bytes) == (
        None,
        len(payload),
        0,
    )
    assert "private-image" not in json.dumps([asdict(record) for record in records])


@pytest.mark.parametrize("save", [False, True])
def test_archive_spawn_failure_has_one_unknown_completion(
    save: bool, tmp_path: Path
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    client.client_config._client_call_with_path = [str(tmp_path / "missing-cli")]

    with (
        patch.object(client.image, "inspect", return_value=[]),
        pytest.raises(FileNotFoundError),
    ):
        if save:
            list(client.image.save("private-image"))
        else:
            client.image.load(b"private-input")

    completed = completion(records)
    assert completed.family == ("archive_save" if save else "archive_load")
    assert completed.outcome == "failure"
    assert (
        completed.stdin_bytes,
        completed.stdout_bytes,
        completed.stderr_bytes,
        completed.return_code,
    ) == (None, None, None, None)


@pytest.mark.parametrize("save", [False, True])
def test_filename_archives_keep_one_existing_native_telemetry_path(
    save: bool, tmp_path: Path
) -> None:
    # Given a filename operation with its existing captured or stream transport.
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", "print('Loaded image: ok')"],
        telemetry_observer=records.append,
    )

    # When that operation completes through the public API.
    with patch.object(client.image, "inspect", return_value=[]):
        if save:
            assert client.image.save("sample", output=tmp_path / "archive.tar") is None
        else:
            assert client.image.load(tmp_path / "archive.tar", quiet=True) == ["ok"]

    # Then there is exactly one pair in the original execution family.
    completed = completion(records)
    assert (completed.family, completed.outcome, completed.return_code) == (
        "captured" if save else "stream",
        "success",
        0,
    )


@pytest.mark.parametrize("save", [False, True])
def test_archive_natural_failure_keeps_original_error_and_counts(save: bool) -> None:
    records: list[TelemetryRecord] = []
    payload = b"private-payload\n"
    code = (
        "import sys; sys.stdin.buffer.read(); sys.exit(7)"
        if not save
        else f"import os; os.write(1,{payload!r}); os.write(2,b'No such image: private'); os._exit(7)"
    )
    client = DockerClient(
        client_call=[sys.executable, "-c", code], telemetry_observer=records.append
    )

    with (
        patch.object(client.image, "inspect", return_value=[]),
        pytest.raises(NoSuchImage if save else DockerException) as raised,
    ):
        if save:
            list(client.image.save("private-image"))
        else:
            client.image.load(payload)

    completed = completion(records)
    assert (completed.outcome, completed.return_code) == ("failure", 7)
    assert (completed.stdin_bytes, completed.stdout_bytes, completed.stderr_bytes) == (
        (None, len(payload), 22) if save else (len(payload), 0, None)
    )
    assert raised.value.stdout is None
    assert raised.value.stderr == ("No such image: private" if save else None)


def test_archive_load_timeout_counts_partial_input() -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import threading; threading.Event().wait()",
        ],
        default_timeout=0.3,
        telemetry_observer=records.append,
    )
    payload = b"x" * (2 * 1024 * 1024)

    with pytest.raises(DockerTimeout):
        client.image.load(payload)

    completed = completion(records)
    assert completed.stdin_bytes is not None and 0 < completed.stdin_bytes < len(
        payload
    )
    assert completed.outcome == "timeout" and completed.return_code is not None


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_archive_load_early_exit_counts_only_successful_pipe_writes(
    timeout: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a child that exits after consuming only one byte of a large archive.
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", "import os; os.read(0,1); os._exit(7)"],
        default_timeout=timeout,
        telemetry_observer=records.append,
    )
    payload = b"x" * (2 * 1024 * 1024)
    written: list[int] = []
    write = os.write

    def measure(fd: int, data: Buffer) -> int:
        count = write(fd, data)
        written.append(count)
        return count

    monkeypatch.setattr(_archive_deadline.os, "write", measure)

    # When the archive's remaining writes encounter the closed pipe.
    with pytest.raises(BrokenPipeError):
        client.image.load(payload)

    # Then only accepted bytes are reported, after the child has been reaped.
    completed = completion(records)
    assert completed.stdin_bytes == sum(written)
    assert 0 < sum(written) < len(payload)
    assert (completed.outcome, completed.return_code) == ("failure", 7)


def budget(limit: int | None, *, stream: int | None = None) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_stream_bytes=stream,
        max_total_stdin_bytes=limit,
    )


LOAD_COUNT = (
    "import sys; data=sys.stdin.buffer.read(); print('Loaded image: '+str(len(data)))"
)


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_archive_load_budget_counts_partial_writes_and_accepts_the_exact_limit(
    timeout: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", LOAD_COUNT],
        default_timeout=timeout,
        telemetry_observer=records.append,
    )
    payload = b"private-archive" * 100
    written: list[int] = []
    write = os.write

    def short_write(fd: int, data: Buffer) -> int:
        count = write(fd, memoryview(data)[:7])
        written.append(count)
        return count

    monkeypatch.setattr(_archive_deadline.os, "write", short_write)

    with observation(budget(len(payload))) as state:
        assert client.image.load(iter((payload[:17], payload[17:]))) == [
            str(len(payload))
        ]

    completed = completion(records)
    assert completed.outcome == "success"
    assert completed.stdin_bytes == sum(written) == len(payload)
    assert state.stdin_accounts[0].consumed == len(payload)
    assert "private-archive" not in json.dumps([asdict(record) for record in records])


@pytest.mark.parametrize("form", ["bytes", "chunks"])
def test_archive_load_past_the_budget_stops_at_the_limit_and_reaps_the_child(
    form: str,
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", LOAD_COUNT],
        telemetry_observer=records.append,
    )
    payload = b"private-archive" * 10
    before = set(threading.enumerate())

    with (
        observation(budget(64)) as state,
        pytest.raises(ObservationLimitError) as raised,
    ):
        client.image.load(
            payload if form == "bytes" else iter((payload[:50], payload[50:]))
        )

    completed = completion(records)
    assert (completed.outcome, completed.stdin_bytes) == ("limit", 64)
    assert completed.return_code is not None
    assert state.stdin_accounts[0].consumed == 64
    assert (raised.value.reason, raised.value.limit, raised.value.measured) == (
        "total_stdin",
        64,
        65,
    )
    assert "private-archive" not in str(raised.value)
    assert "private-archive" not in json.dumps([asdict(record) for record in records])
    assert set(threading.enumerate()) - before == set()


def test_competing_archive_loads_share_one_budget_and_never_pass_it() -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", LOAD_COUNT],
        telemetry_observer=records.append,
    )
    gate = Barrier(2)

    def upload() -> Iterator[bytes]:
        yield b"a" * 30
        # Both uploads have had their first chunk accepted before either goes on.
        gate.wait(timeout=10)
        yield b"b" * 40

    with observation(budget(100)) as state, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(copy_context().run, client.image.load, upload())
            for _ in range(2)
        ]
        errors = [future.exception(timeout=30) for future in futures]

    assert sorted(type(error).__name__ for error in errors) == [
        "NoneType",
        "ObservationLimitError",
    ]
    assert sorted(
        done.stdin_bytes or 0 for done in records if isinstance(done, CompletionRecord)
    ) == [30, 70]
    assert state.stdin_accounts[0].consumed == 100


def test_archive_load_early_exit_with_a_budget_counts_only_accepted_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", "import os; os.read(0,1); os._exit(7)"],
        telemetry_observer=records.append,
    )
    payload = b"x" * (2 * 1024 * 1024)
    written: list[int] = []
    write = os.write

    def measure(fd: int, data: Buffer) -> int:
        count = write(fd, data)
        written.append(count)
        return count

    monkeypatch.setattr(_archive_deadline.os, "write", measure)

    with (
        observation(budget(len(payload))) as state,
        pytest.raises(BrokenPipeError),
    ):
        client.image.load(payload)

    completed = completion(records)
    assert completed.stdin_bytes == sum(written) == state.stdin_accounts[0].consumed
    assert 0 < sum(written) < len(payload)
    assert (completed.outcome, completed.return_code) == ("failure", 7)


def test_archive_load_charges_input_and_output_to_separate_accounts() -> None:
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(); sys.stdout.write('Loaded image: sample\\n')",
        ]
    )

    with observation(budget(4096, stream=21)) as state:
        assert client.image.load(b"x" * 4096) == ["sample"]

    assert state.stdin_accounts[0].consumed == 4096
    assert state.stream_accounts[0].consumed == 21


def test_archive_owner_keeps_charging_its_stdin_accounts_after_the_scope_exits() -> (
    None
):
    command = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
    with Popen(command, stdin=PIPE, stdout=PIPE, bufsize=0) as process:
        assert process.stdin is not None
        with observation(budget(10)) as state:
            owner = ArchiveDeadline(
                process, command, None, raw=False, observation=state
            )
        try:
            assert owner.bounded

            owner.write(process.stdin, b"x" * 6)
            with pytest.raises(ObservationLimitError) as raised:
                owner.write(process.stdin, b"y" * 6)
        finally:
            process.kill()
            owner.thread.join()

    assert (raised.value.reason, raised.value.limit, raised.value.measured) == (
        "total_stdin",
        10,
        11,
    )
    assert state.stdin_accounts[0].consumed == owner.stdin_bytes == 10
    assert process.returncode is not None


def test_archive_load_producer_failure_keeps_progress_and_original_error() -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        telemetry_observer=records.append,
    )
    original = ValueError("private-producer-error")

    def chunks() -> Generator[bytes]:
        yield b"accepted"
        raise original

    with pytest.raises(ValueError) as raised:
        client.image.load(chunks())

    completed = completion(records)
    assert raised.value is original
    assert (completed.stdin_bytes, completed.outcome) == (8, "failure")
    assert completed.return_code is not None


def test_archive_observers_are_isolated_and_snapshotted_at_launch() -> None:
    records: list[list[TelemetryRecord]] = [[], []]
    later: list[TelemetryRecord] = []
    clients = [
        DockerClient(
            client_call=[
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read(); print('Loaded image: ok')",
            ],
            telemetry_observer=observer.append,
        )
        for observer in records
    ]

    def load(client: DockerClient) -> list[str]:
        def chunks() -> Generator[bytes]:
            client.client_config.telemetry_observer = later.append
            yield b"archive"

        return client.image.load(chunks())

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(load, clients)) == [["ok"], ["ok"]]

    assert later == []
    first, second = (completion(observer) for observer in records)
    assert first.attempt_id != second.attempt_id
    assert first.stdin_bytes == second.stdin_bytes == 7
