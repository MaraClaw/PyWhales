import errno
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from subprocess import Popen
from threading import Event
from time import monotonic
from types import GeneratorType
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._archive_deadline import ArchiveDeadline
from pywhales._observation import ObservationState, observation
from pywhales.exceptions import (
    DockerTimeout,
    NoSuchImage,
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


def test_unstarted_archive_save_close_has_no_launch_or_telemetry() -> None:
    # Given an archive iterator whose command has not started.
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch("pywhales.components.image.cli_wrapper.Popen") as launch,
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)

        # When the unused iterator is closed.
        archive.close()

    # Then it remains inert.
    launch.assert_not_called()
    assert records == []


@pytest.mark.parametrize("timeout", [None, 3.0])
def test_archive_save_close_completes_after_pipe_process_and_thread_cleanup(
    timeout: float | None,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    cleanup: list[bool] = []

    def observe(record: TelemetryRecord) -> None:
        records.append(record)
        if isinstance(record, CompletionRecord):
            owner = owners[0]
            cleanup.append(
                owner.process.returncode is not None
                and not owner.thread.is_alive()
                and all(
                    pipe.closed
                    for pipe in (owner.process.stdout, owner.process.stderr)
                    if pipe is not None
                )
            )

    def make_owner(
        process: Popen[bytes], command: list[str], deadline: float | None, *, raw: bool
    ) -> ArchiveDeadline:
        owner = ArchiveDeadline(process, command, deadline, raw=raw)
        owners.append(owner)
        return owner

    code = "import os,threading; os.write(1,b'first\\n'); threading.Event().wait()"
    client = DockerClient(
        client_call=[sys.executable, "-c", code],
        default_timeout=timeout,
        telemetry_observer=observe,
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch("pywhales.components.image.cli_wrapper.ArchiveDeadline", make_owner),
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)
        try:
            assert next(archive) == b"first\n"
            assert len(records) == 1

            archive.close()
        finally:
            archive.close()

    assert cleanup == [True]
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdin_bytes, completed.stdout_bytes) == (
        "closed",
        None,
        6,
    )
    assert list(archive) == []


def test_paused_archive_save_timeout_counts_prior_reads_once_after_cleanup() -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []

    def make_owner(
        process: Popen[bytes], command: list[str], deadline: float | None, *, raw: bool
    ) -> ArchiveDeadline:
        owner = ArchiveDeadline(process, command, deadline, raw=raw)
        owners.append(owner)
        return owner

    code = "import os,threading; os.write(2,b'error'); os.write(1,b'first\\n'); threading.Event().wait()"
    client = DockerClient(
        client_call=[sys.executable, "-c", code],
        default_timeout=0.3,
        telemetry_observer=records.append,
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch("pywhales.components.image.cli_wrapper.ArchiveDeadline", make_owner),
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)
        try:
            assert next(archive) == b"first\n"
            client.client_config.default_timeout = 30
            owner = owners[0]
            assert owner.released.wait(3)
            assert owner.process.returncode is not None
            assert len(records) == 1

            with pytest.raises(DockerTimeout) as raised:
                next(archive)
        finally:
            archive.close()

    assert raised.value.stderr == "error"
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "timeout",
        6,
        5,
    )
    assert not owner.thread.is_alive()
    assert all(
        pipe.closed
        for pipe in (owner.process.stdout, owner.process.stderr)
        if pipe is not None
    )
    assert list(archive) == [] and len(records) == 2


def test_stalled_archive_load_finishes_telemetry_only_when_producer_resumes() -> None:
    records: list[TelemetryRecord] = []
    owners: Queue[ArchiveDeadline] = Queue()
    entered = Event()
    resume = Event()

    def make_owner(
        process: Popen[bytes], command: list[str], deadline: float | None, *, raw: bool
    ) -> ArchiveDeadline:
        owner = ArchiveDeadline(process, command, deadline, raw=raw)
        owners.put(owner)
        return owner

    def chunks() -> Generator[bytes]:
        yield b"accepted"
        entered.set()
        assert resume.wait(3)
        yield b""
        raise AssertionError("producer advanced after deadline")

    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import threading; threading.Event().wait()",
        ],
        default_timeout=0.3,
        telemetry_observer=records.append,
    )
    with (
        patch("pywhales.components.image.cli_wrapper.ArchiveDeadline", make_owner),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(client.image.load, chunks())
        try:
            owner = owners.get(timeout=3)
            assert entered.wait(3) and owner.released.wait(3)
            assert owner.process.returncode is not None
            assert not result.done() and len(records) == 1
        finally:
            resume.set()

        with pytest.raises(DockerTimeout):
            result.result(timeout=3)

    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdin_bytes) == ("timeout", 8)
    assert not owner.thread.is_alive()
    assert owner.process.stdin is not None and owner.process.stdin.closed


_FIRST_THEN_BLOCK = (
    "import os,threading; os.write(1,b'first\\n'); threading.Event().wait()"
)


def scope_policy(
    argv: int = 65536, stream: int | None = None, *, diagnostics: int | None = None
) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1,
        max_argv_bytes=argv,
        max_response_bytes=1024,
        max_total_stream_bytes=stream,
        max_total_archive_stderr_bytes=diagnostics,
    )


def archive_client(
    records: list[TelemetryRecord], code: str = _FIRST_THEN_BLOCK
) -> DockerClient:
    return DockerClient(
        client_call=[sys.executable, "-c", code], telemetry_observer=records.append
    )


def capture_owners(owners: list[ArchiveDeadline]) -> type[ArchiveDeadline]:
    """Return an ArchiveDeadline stand-in that records each owner it creates."""

    class RecordingOwner(ArchiveDeadline):
        def __init__(
            self,
            process: Popen[bytes],
            command: list[str],
            deadline: float | None,
            *,
            raw: bool,
            observation: ObservationState | None = None,
        ) -> None:
            super().__init__(
                process, command, deadline, raw=raw, observation=observation
            )
            owners.append(self)

    return RecordingOwner


def assert_owner_released(owner: ArchiveDeadline) -> None:
    """Check the child was reaped, its pipes closed, and the accounting thread joined."""
    assert owner.process.returncode is not None
    assert not owner.thread.is_alive()
    assert all(
        pipe.closed
        for pipe in (owner.process.stdin, owner.process.stdout, owner.process.stderr)
        if pipe is not None
    )


@pytest.mark.parametrize("diagnostics", [None, 4])
def test_lazy_save_applies_the_scope_active_at_first_iteration_and_keeps_it(
    diagnostics: int | None,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    cancel = Event()
    client = archive_client(records)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        # Given a save created outside any scope and first iterated inside one.
        archive = client.image.save("sample")
        with observation(scope_policy(diagnostics=diagnostics), cancel=cancel):
            assert next(archive) == b"first\n"
        (owner,) = owners
        assert len(records) == 1

        # When the token is set after that scope has exited.
        cancel.set()

        # Then the transfer still stops, is reaped, and completes telemetry once.
        assert owner.released.wait(3)
        with pytest.raises(ObservationCancelled):
            next(archive)
        assert list(archive) == []
    assert_owner_released(owner)
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdout_bytes) == ("cancelled", 6)


@pytest.mark.parametrize("diagnostics", [None, 4])
def test_lazy_save_ignores_a_scope_that_only_surrounded_its_creation(
    diagnostics: int | None,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    cancel = Event()
    client = archive_client(
        records, "import os; os.write(1,b'first\\n'); os.write(1,b'second\\n')"
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        with observation(scope_policy(diagnostics=diagnostics), cancel=cancel):
            archive = client.image.save("sample")
        cancel.set()

        assert b"".join(archive) == b"first\nsecond\n"

    (owner,) = owners
    assert not owner.bounded
    assert_owner_released(owner)
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == "success"


@pytest.mark.parametrize("operation", ["save", "load"])
@pytest.mark.parametrize("stop", ["cancelled", "argv"])
def test_archive_launch_is_rejected_before_input_and_process(
    operation: str, stop: str
) -> None:
    records: list[TelemetryRecord] = []
    consumed: list[bool] = []
    cancel = Event()
    client = archive_client(records)

    def chunks() -> Generator[bytes]:
        consumed.append(True)
        yield b"input"

    def attempt() -> object:
        if operation == "load":
            return client.image.load(chunks())
        return next(client.image.save("sample"))

    expected = ObservationCancelled if stop == "cancelled" else ObservationLimitError
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch("pywhales.components.image.cli_wrapper.Popen") as launch,
        pytest.raises(expected),
        observation(scope_policy(argv=8 if stop == "argv" else 65536), cancel=cancel),
    ):
        if stop == "cancelled":
            cancel.set()
        attempt()

    launch.assert_not_called()
    assert consumed == []
    (record,) = records
    assert isinstance(record, RefusalRecord)
    assert record.family == ("archive_load" if operation == "load" else "archive_save")
    assert record.outcome == ("cancelled" if stop == "cancelled" else "limit")


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_scope_stop_reaps_a_paused_save_and_finishes_telemetry_once(
    stop: str,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    cancel = Event()
    client = archive_client(records)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        archive = client.image.save("sample")
        with observation(
            scope_policy(),
            deadline=monotonic() + 0.4 if stop == "deadline" else None,
            cancel=cancel,
        ):
            assert next(archive) == b"first\n"
        (owner,) = owners
        if stop == "cancel":
            cancel.set()

        assert owner.released.wait(3)
        assert len(records) == 1
        with pytest.raises(
            ObservationTimeout if stop == "deadline" else ObservationCancelled
        ):
            next(archive)
        assert list(archive) == []

    assert_owner_released(owner)
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == ("timeout" if stop == "deadline" else "cancelled")
    assert (completed.stdout_bytes, completed.return_code) == (
        6,
        owner.process.returncode,
    )


@pytest.mark.parametrize("stop", ["deadline", "cancel", "budget", "diagnostics"])
def test_early_save_close_inside_a_live_scope_cleans_up_and_closes(stop: str) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    client = archive_client(records)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)
        with observation(
            scope_policy(
                stream=1024 if stop == "budget" else None,
                diagnostics=1024 if stop == "diagnostics" else None,
            ),
            deadline=monotonic() + 30 if stop == "deadline" else None,
            cancel=Event() if stop in {"deadline", "cancel"} else None,
        ):
            assert next(archive) == b"first\n"
            archive.close()
            archive.close()

    (owner,) = owners
    assert_owner_released(owner)
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == "closed"
    assert list(archive) == []


@pytest.mark.parametrize("scope_first", [True, False])
@pytest.mark.parametrize("diagnostics", [None, 4])
def test_save_command_timeout_precedes_only_a_later_scope_deadline(
    scope_first: bool,
    diagnostics: int | None,
) -> None:
    owners: list[ArchiveDeadline] = []
    client = archive_client([])
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        archive = client.image.save("sample", timeout=30 if scope_first else 0.4)
        with observation(
            scope_policy(diagnostics=diagnostics),
            deadline=monotonic() + (0.4 if scope_first else 30),
        ):
            assert next(archive) == b"first\n"
        (owner,) = owners
        assert owner.released.wait(3)

        with pytest.raises(ObservationTimeout if scope_first else DockerTimeout):
            next(archive)

    assert_owner_released(owner)


@pytest.mark.parametrize("diagnostics", [None, 4])
def test_scope_cancel_reaps_a_load_while_its_producer_is_stalled(
    diagnostics: int | None,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    cancel = Event()
    client = archive_client(records, "import threading; threading.Event().wait()")

    def chunks() -> Generator[bytes]:
        yield b"accepted"
        cancel.set()
        assert owners[0].released.wait(3)
        yield b""
        raise AssertionError("producer advanced after cancellation")

    with (
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
        pytest.raises(ObservationCancelled),
        observation(scope_policy(diagnostics=diagnostics), cancel=cancel),
    ):
        client.image.load(chunks())

    (owner,) = owners
    assert_owner_released(owner)
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdin_bytes) == ("cancelled", 8)


def test_scope_deadline_interrupts_a_load_with_a_saturated_stdin_pipe() -> None:
    owners: list[ArchiveDeadline] = []
    client = archive_client([], "import time; time.sleep(60)")
    with (
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
        pytest.raises(ObservationTimeout),
        observation(scope_policy(), deadline=monotonic() + 0.4),
    ):
        client.image.load(b"x" * (2 * 1024 * 1024))

    (owner,) = owners
    assert_owner_released(owner)


def test_path_load_inside_a_scope_stops_when_the_scope_is_cancelled() -> None:
    cancel = Event()

    def observe(record: TelemetryRecord) -> None:
        if isinstance(record, LaunchRecord):
            cancel.set()

    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import threading; print('Loaded image: x', flush=True); threading.Event().wait()",
        ],
        telemetry_observer=observe,
    )
    with (
        pytest.raises(ObservationCancelled),
        observation(scope_policy(), cancel=cancel),
    ):
        client.image.load("archive.tar", quiet=True)


@pytest.mark.parametrize(("limit", "fits"), [(6, True), (5, False)])
def test_save_stream_budget_boundary_is_the_exact_archive_size(
    limit: int, fits: bool
) -> None:
    records: list[TelemetryRecord] = []
    client = archive_client(records, "import os; os.write(1,b'first\\n')")
    with (
        patch.object(client.image, "inspect", return_value=[]),
        observation(scope_policy(stream=limit)) as state,
    ):
        archive = client.image.save("sample")
        if fits:
            assert b"".join(archive) == b"first\n"
        else:
            with pytest.raises(ObservationLimitError) as failure:
                next(archive)
            assert (failure.value.reason, failure.value.limit) == ("total_stream", 5)
            assert failure.value.measured == 6

    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert len(records) == 2
    assert completed.outcome == ("success" if fits else "limit")
    assert state.stream_accounts[0].consumed == 6


def test_save_overflow_reaps_the_child_and_never_charges_the_watchdog_drain() -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    client = archive_client(records)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
        observation(scope_policy(stream=4)) as state,
        pytest.raises(ObservationLimitError),
    ):
        archive = client.image.save("sample")
        try:
            next(archive)
        finally:
            assert list(archive) == []

    (owner,) = owners
    assert_owner_released(owner)
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.return_code) == (
        "limit",
        owner.process.returncode,
    )
    # The caller read five bytes (the limit plus one); the drain after the kill took the
    # sixth for telemetry only.
    assert completed.stdout_bytes == 6
    assert state.stream_accounts[0].consumed == 5


def test_budget_only_save_outlives_its_scope_and_charges_later_reads(
    tmp_path: Path,
) -> None:
    go = tmp_path / "go"
    code = (
        "import os,time; os.write(1,b'first\\n')\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.01)\n"
        "os.write(1,b'second\\n'); time.sleep(60)"
    )
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    client = archive_client(records, code)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        archive = client.image.save("sample")
        with observation(scope_policy(stream=8)) as state:
            assert next(archive) == b"first\n"
        (owner,) = owners

        assert owner.bounded and owner._remaining() is None
        assert state.stream_accounts[0].consumed == 6
        go.touch()
        with pytest.raises(ObservationLimitError) as failure:
            next(archive)

    assert (failure.value.limit, failure.value.measured) == (8, 9)
    assert_owner_released(owner)
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == "limit"


def test_stderr_selected_save_failure_wins_over_the_stream_budget() -> None:
    records: list[TelemetryRecord] = []
    code = (
        "import os; os.write(1,b'first\\n'); "
        "os.write(2,b'No such image: sample and a long explanation'); os._exit(1)"
    )
    client = archive_client(records, code)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        observation(scope_policy(stream=6)) as state,
        pytest.raises(NoSuchImage),
    ):
        list(client.image.save("sample"))

    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.return_code) == ("failure", 1)
    assert completed.stderr_bytes == 44
    assert state.stream_accounts[0].consumed == 6


@pytest.mark.parametrize(("limit", "fits"), [(21, True), (20, False)])
def test_load_stream_budget_charges_only_the_output_read(
    limit: int, fits: bool
) -> None:
    records: list[TelemetryRecord] = []
    code = "import sys; sys.stdin.buffer.read(); sys.stdout.write('Loaded image: sample\\n')"
    client = archive_client(records, code)
    with observation(scope_policy(stream=limit)) as state:
        if fits:
            assert client.image.load(b"x" * 4096) == ["sample"]
        else:
            with pytest.raises(ObservationLimitError) as failure:
                client.image.load(b"x" * 4096)
            assert (failure.value.limit, failure.value.measured) == (20, 21)

    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert len(records) == 2
    assert completed.outcome == ("success" if fits else "limit")
    assert completed.stdin_bytes == 4096
    assert state.stream_accounts[0].consumed == 21


@pytest.mark.parametrize("operation", ["save", "load"])
def test_archive_native_argv_rejection_after_start_reports_limit(
    operation: str,
) -> None:
    records: list[TelemetryRecord] = []
    client = archive_client(records)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.Popen",
            side_effect=OSError(errno.E2BIG, "too big"),
        ),
        pytest.raises(ObservationLimitError) as failure,
        observation(scope_policy()),
    ):
        if operation == "load":
            client.image.load(b"input")
        else:
            next(client.image.save("sample"))

    assert failure.value.reason == "native_argv"
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.return_code) == ("limit", None)


@pytest.mark.parametrize("delivery", ["next", "close"])
def test_paused_archive_diagnostic_overflow_is_delivered_once_after_scope_exit(
    delivery: str,
    tmp_path: Path,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    go = tmp_path / "go"
    code = (
        "import os,time,threading; os.write(1,b'first')\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.001)\n"
        "os.write(2,b'No such image: private' * 10000); threading.Event().wait()"
    )
    client = archive_client(records, code)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)
        with observation(scope_policy(diagnostics=4)) as state:
            assert next(archive) == b"first"
        (owner,) = owners
        go.touch()
        assert owner.released.wait(timeout=3)
        assert len(records) == 1
        assert owner.stderr_bytes == 5 and owner.stderr == b"No s"
        assert state.archive_stderr_accounts[0].consumed == 5
        assert owner._scope is state and owner._remaining() is None
        with pytest.raises(ObservationLimitError) as failure:
            if delivery == "next":
                next(archive)
            else:
                archive.close()
        archive.close()
        assert list(archive) == []

    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "total_archive_stderr",
        4,
        5,
    )
    assert_owner_released(owner)
    assert all(not reader.is_alive() for reader in owner._readers)
    assert owner._wake is not None and owner._wake.fileno() is None
    assert len(records) == 2
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stop_reason, completed.stderr_bytes) == (
        "limit",
        "other_limit",
        5,
    )
    assert "private" not in repr(records)


def test_paused_save_drains_exact_diagnostic_allowance_before_stdout_closes(
    tmp_path: Path,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    go = tmp_path / "go"
    code = (
        "import os,time; os.write(1,b'\\x00\\xfffirst')\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.001)\n"
        "os.write(2,b'e'*131072); os.write(1,b'second'); os._exit(0)"
    )
    client = archive_client(records, code)
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
        observation(scope_policy(diagnostics=131072)) as state,
    ):
        archive = client.image.save("sample")
        assert isinstance(archive, GeneratorType)
        try:
            assert next(archive) == b"\x00\xfffirst"
            (owner,) = owners
            go.touch()
            assert owner._exited.wait(timeout=3)
            assert b"".join(archive) == b"second"
        finally:
            archive.close()

    assert state.archive_stderr_accounts[0].consumed == 131072
    assert owner.stderr == b"e" * 131072
    assert_owner_released(owner)
    assert all(not reader.is_alive() for reader in owner._readers)
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (len(records), completed.outcome, completed.stderr_bytes) == (
        2,
        "success",
        131072,
    )


def test_archive_diagnostic_cleanup_preserves_a_stalled_producers_original_error(
    tmp_path: Path,
) -> None:
    records: list[TelemetryRecord] = []
    owners: list[ArchiveDeadline] = []
    go = tmp_path / "go"
    code = (
        "import os,time,threading\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.001)\n"
        "os.write(2,b'overflow'); threading.Event().wait()"
    )
    error = RuntimeError("original producer error")
    client = archive_client(records, code)

    def chunks() -> Generator[bytes]:
        yield b"input"
        go.touch()
        assert owners[0].released.wait(timeout=3)
        raise error

    with (
        patch(
            "pywhales.components.image.cli_wrapper.ArchiveDeadline",
            capture_owners(owners),
        ),
        observation(scope_policy(diagnostics=4)),
        pytest.raises(RuntimeError) as failure,
    ):
        client.image.load(chunks())

    assert failure.value is error
    (owner,) = owners
    assert_owner_released(owner)
    assert all(not reader.is_alive() for reader in owner._readers)
    completed = records[-1]
    assert isinstance(completed, CompletionRecord)
    assert (len(records), completed.outcome, completed.stop_reason) == (
        2,
        "failure",
        None,
    )
