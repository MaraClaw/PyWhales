import os
import select
import signal
import sys
import threading
from collections import Counter
from collections.abc import Buffer, Generator
from functools import partial
from subprocess import PIPE, Popen
from threading import Event, Thread
from time import monotonic, sleep
from typing import Literal, override

import pytest

from pywhales import DockerClient, _archive_deadline
from pywhales._archive_deadline import ArchiveDeadline
from pywhales._observation import ObservationState, observation
from pywhales._readiness import CANCEL_CHECK, WakeLatch
from pywhales.components.image import cli_wrapper
from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from tests.pywhales.process_groups import Grandchild, holder, stdin_from

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX readiness waits")

_HOLDER = (
    "import os,sys\n"
    "release,status=map(int,sys.argv[1:3])\n"
    "if os.fork()==0:\n"
    " os.write(status,b'R'); os.read(release,1); os._exit(0)\n"
)


def test_deadline_read_waits_for_the_remaining_time_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a silent child and a deadline for the owner.
    attempts: Counter[int] = Counter()
    read = os.read

    def counted(fd: int, size: int) -> bytes:
        attempts[fd] += 1
        return read(fd, size)

    monkeypatch.setattr(os, "read", counted)
    command = [sys.executable, "-c", "import threading; threading.Event().wait()"]
    before = set(threading.enumerate())
    with Popen(command, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        fd = process.stdout.fileno()
        owner = ArchiveDeadline(process, command, monotonic() + 0.8, raw=False)
        started = monotonic()

        # When a read waits for output that never comes.
        with pytest.raises(DockerTimeout):
            owner.read(process.stdout)

        # Then it waited out the deadline in one wait and the owner cleaned up.
        assert 0.6 < monotonic() - started < 3
        assert attempts[fd] <= 3
        owner.thread.join(timeout=3)
        assert owner.released.is_set() and process.returncode is not None
        assert process.stdout.closed
    assert set(threading.enumerate()) == before


def test_deadline_write_waits_for_the_remaining_time_with_exact_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    with Popen(command, stdin=PIPE, bufsize=0) as process:
        assert process.stdin is not None
        fd = process.stdin.fileno()
        owner = ArchiveDeadline(process, command, monotonic() + 0.8, raw=False)
        started = monotonic()

        with pytest.raises(DockerTimeout):
            owner.write(process.stdin, b"x" * (8 * 1024 * 1024))

        assert 0.6 < monotonic() - started < 3
        assert 0 < blocked[fd] <= 3
        assert owner.stdin_bytes == accepted[fd] > 0
        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL and process.stdin.closed


def test_deadline_stops_a_read_while_a_descendant_holds_the_pipe() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    command = [
        sys.executable,
        "-c",
        _HOLDER + "import time; time.sleep(60)\n",
        str(release_read),
        str(status_write),
    ]
    try:
        with Popen(
            command, stdout=PIPE, bufsize=0, pass_fds=(release_read, status_write)
        ) as process:
            assert process.stdout is not None
            owner = ArchiveDeadline(process, command, monotonic() + 0.5, raw=False)
            assert select.select([status_read], [], [], 3)[0]
            assert os.read(status_read, 1) == b"R"

            outcome: list[bytes | BaseException] = []

            def consume() -> None:
                assert process.stdout is not None
                try:
                    outcome.append(owner.read(process.stdout))
                except DockerTimeout as error:
                    outcome.append(error)

            reader = Thread(target=consume, daemon=True)
            reader.start()
            reader.join(timeout=3)

            assert not reader.is_alive(), "read waited for the descendant"
            assert [type(item) for item in outcome] == [DockerTimeout]
            owner.thread.join(timeout=3)
            assert not owner.thread.is_alive() and process.stdout.closed
    finally:
        os.write(release_write, b"x")
        for descriptor in (release_read, release_write, status_write):
            os.close(descriptor)
        assert os.read(status_read, 1) == b""
        os.close(status_read)


def test_read_after_natural_exit_ignores_the_passed_deadline_without_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: Counter[int] = Counter()
    read = os.read

    def counted(fd: int, size: int) -> bytes:
        attempts[fd] += 1
        return read(fd, size)

    monkeypatch.setattr(os, "read", counted)
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    command = [
        sys.executable,
        "-c",
        _HOLDER + "os._exit(0)\n",
        str(release_read),
        str(status_write),
    ]
    outcome: list[bytes | BaseException] = []
    try:
        with Popen(
            command, stdout=PIPE, bufsize=0, pass_fds=(release_read, status_write)
        ) as process:
            assert process.stdout is not None
            fd = process.stdout.fileno()
            owner = ArchiveDeadline(process, command, monotonic() + 0.3, raw=False)

            def consume() -> None:
                assert process.stdout is not None
                try:
                    outcome.append(owner.read(process.stdout))
                except DockerTimeout as error:
                    outcome.append(error)

            reader = Thread(target=consume, daemon=True)
            reader.start()
            assert select.select([status_read], [], [], 3)[0]
            assert os.read(status_read, 1) == b"R"

            # When the deadline passes after the child exited on its own.
            sleep(0.8)

            # Then the read keeps waiting for the descendant, untimed.
            assert reader.is_alive() and not outcome and not owner.expired
            assert attempts[fd] <= 3

            # And reaches EOF once the descendant lets go.
            os.write(release_write, b"x")
            reader.join(timeout=3)
            assert outcome == [b""]
            assert not owner.expired
    finally:
        os.write(release_write, b"x")
        for descriptor in (release_read, release_write, status_write):
            os.close(descriptor)
        assert os.read(status_read, 1) == b""
        os.close(status_read)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_owner_start_failure_kills_the_child_and_joins_its_helper(
    fail_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = 0

    class FailingThread(Thread):
        @override
        def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts == fail_at:
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(_archive_deadline, "Thread", FailingThread)
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    before = set(threading.enumerate())
    with Popen(command, stdout=PIPE, bufsize=0) as process:
        with pytest.raises(RuntimeError):
            ArchiveDeadline(process, command, monotonic() + 30, raw=False)

        assert process.wait(timeout=3) == -signal.SIGKILL
    assert set(threading.enumerate()) == before


def scope_state(
    *, deadline: float | None = None, cancel: Event | None = None
) -> ObservationState:
    """Return the state of a scope that has already exited, as a launch keeps it."""
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )
    with observation(policy, deadline=deadline, cancel=cancel) as state:
        return state


_BLOCKER = [sys.executable, "-c", "import threading; threading.Event().wait()"]


def test_scope_deadline_reaps_a_silent_child_and_check_reports_it() -> None:
    before = set(threading.enumerate())
    state = scope_state(deadline=monotonic() + 0.5)
    with Popen(_BLOCKER, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        owner = ArchiveDeadline(process, _BLOCKER, None, raw=False, observation=state)

        with pytest.raises(ObservationTimeout):
            owner.read(process.stdout)

        assert owner.expired and owner.released.is_set()
        with pytest.raises(ObservationTimeout):
            owner.check()
        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL
        assert process.stdout.closed
    assert set(threading.enumerate()) == before


def test_cancel_token_interrupts_a_blocked_read_with_only_bounded_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float | None] = []
    blocked = Event()
    wait_ready = _archive_deadline.wait_ready

    def record(
        fd: int,
        *,
        writable: bool = False,
        latch: WakeLatch | None = None,
        timeout: float | None = None,
    ) -> None:
        timeouts.append(timeout)
        blocked.set()
        wait_ready(fd, writable=writable, latch=latch, timeout=timeout)

    monkeypatch.setattr(_archive_deadline, "wait_ready", record)
    cancel = Event()
    state = scope_state(cancel=cancel)
    with Popen(_BLOCKER, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        owner = ArchiveDeadline(process, _BLOCKER, None, raw=False, observation=state)
        outcome: list[bytes | BaseException] = []

        def consume() -> None:
            assert process.stdout is not None
            try:
                outcome.append(owner.read(process.stdout))
            except ObservationCancelled as error:
                outcome.append(error)

        reader = Thread(target=consume, daemon=True)
        reader.start()
        assert blocked.wait(timeout=3)

        cancel.set()
        reader.join(timeout=3)

        assert not reader.is_alive()
        assert [type(item) for item in outcome] == [ObservationCancelled]
        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL and process.stdout.closed
    assert timeouts
    assert all(timeout is not None and timeout <= CANCEL_CHECK for timeout in timeouts)


def test_cancel_token_interrupts_a_saturated_write() -> None:
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    cancel = Event()
    state = scope_state(cancel=cancel)
    with Popen(command, stdin=PIPE, bufsize=0) as process:
        assert process.stdin is not None
        owner = ArchiveDeadline(process, command, None, raw=False, observation=state)
        cancel.set()

        with pytest.raises(ObservationCancelled):
            owner.write(process.stdin, b"x" * (8 * 1024 * 1024))

        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL and process.stdin.closed


@pytest.mark.parametrize("command_first", [True, False])
def test_command_deadline_precedes_only_a_later_scope_deadline(
    command_first: bool,
) -> None:
    expected = DockerTimeout if command_first else ObservationTimeout
    now = monotonic()
    state = scope_state(deadline=now + (30 if command_first else 0.4))
    with Popen(_BLOCKER, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        owner = ArchiveDeadline(
            process,
            _BLOCKER,
            now + (0.4 if command_first else 30),
            raw=False,
            observation=state,
        )

        with pytest.raises(expected):
            owner.read(process.stdout)

        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL


def test_equal_command_and_scope_deadlines_report_the_scope() -> None:
    deadline = monotonic() + 0.4
    state = scope_state(deadline=deadline)
    with Popen(_BLOCKER, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        owner = ArchiveDeadline(
            process, _BLOCKER, deadline, raw=False, observation=state
        )

        with pytest.raises(ObservationTimeout):
            owner.read(process.stdout)


def test_cancel_token_wins_over_expired_command_and_scope_deadlines() -> None:
    cancel = Event()
    cancel.set()
    past = monotonic() - 1
    state = ObservationState(
        policy=InspectionPolicy(
            max_references=1, max_argv_bytes=65536, max_response_bytes=1024
        ),
        deadline=past,
        cancel_tokens=(cancel,),
        response_accounts=(),
    )
    with Popen(_BLOCKER, stdout=PIPE, bufsize=0) as process:
        assert process.stdout is not None
        owner = ArchiveDeadline(process, _BLOCKER, past, raw=False, observation=state)

        with pytest.raises(ObservationCancelled):
            owner.read(process.stdout)

        owner.thread.join(timeout=3)
        assert process.returncode == -signal.SIGKILL


def test_check_reports_a_scope_that_ended_after_a_natural_exit() -> None:
    cancel = Event()
    state = scope_state(cancel=cancel)
    command = [sys.executable, "-c", "pass"]
    with Popen(command, stdout=PIPE, bufsize=0) as process:
        owner = ArchiveDeadline(process, command, None, raw=False, observation=state)
        owner.thread.join(timeout=3)
        assert owner.released.is_set() and not owner.expired
        owner.check()

        cancel.set()

        with pytest.raises(ObservationCancelled):
            owner.check()
        assert process.returncode == 0


def test_scope_deadline_stops_a_read_while_a_descendant_holds_the_pipe() -> None:
    release_read, release_write = os.pipe()
    status_read, status_write = os.pipe()
    command = [
        sys.executable,
        "-c",
        _HOLDER + "os._exit(0)\n",
        str(release_read),
        str(status_write),
    ]
    state = scope_state(deadline=monotonic() + 0.6)
    try:
        with Popen(
            command, stdout=PIPE, bufsize=0, pass_fds=(release_read, status_write)
        ) as process:
            assert process.stdout is not None
            owner = ArchiveDeadline(
                process, command, None, raw=False, observation=state
            )
            assert select.select([status_read], [], [], 3)[0]
            assert os.read(status_read, 1) == b"R"
            outcome: list[bytes | BaseException] = []

            def consume() -> None:
                assert process.stdout is not None
                try:
                    outcome.append(owner.read(process.stdout))
                except ObservationTimeout as error:
                    outcome.append(error)

            reader = Thread(target=consume, daemon=True)
            reader.start()
            reader.join(timeout=3)

            assert not reader.is_alive(), "read waited for the descendant"
            assert [type(item) for item in outcome] == [ObservationTimeout]
            assert not owner.expired and process.returncode == 0
    finally:
        os.write(release_write, b"x")
        for descriptor in (release_read, release_write, status_write):
            os.close(descriptor)
        assert os.read(status_read, 1) == b""
        os.close(status_read)


def test_policy_only_scope_adds_no_monitoring() -> None:
    state = scope_state()
    command = [sys.executable, "-c", "print('x')"]
    with Popen(command, stdout=PIPE, bufsize=0) as process:
        owner = ArchiveDeadline(process, command, None, raw=False, observation=state)

        assert not owner.bounded
        assert process.stdout is not None
        assert b"".join(owner.chunks(process.stdout)) == b"x\n"
        owner.thread.join(timeout=3)


def _archive_client(
    code: str, grandchild: Grandchild, monkeypatch: pytest.MonkeyPatch
) -> DockerClient:
    """Return a client whose archive CLI runs ``code`` with the grandchild's pipes."""
    monkeypatch.setattr(cli_wrapper, "Popen", partial(Popen, pass_fds=grandchild.fds))
    client = DockerClient(client_call=[sys.executable, "-c", code, *grandchild.argv])
    monkeypatch.setattr(client.image, "inspect", lambda images: [])
    return client


_STOPS: dict[str, type[Exception]] = {
    "deadline": DockerTimeout,
    "scope": ObservationTimeout,
    "cancel": ObservationCancelled,
    "limit": ObservationLimitError,
}


@pytest.mark.parametrize("stop", ["deadline", "scope", "cancel", "limit"])
def test_save_stop_kills_the_cli_process_group(
    stop: Literal["deadline", "scope", "cancel", "limit"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Event()
    policy = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_stream_bytes=16,
    )
    with (
        Grandchild() as grandchild,
        open(os.devnull, "rb") as null,
        stdin_from(null.fileno()),
    ):
        # Given a save whose CLI, without a terminal on stdin, has a grandchild
        # holding stdout and stderr.
        size = 64 if stop == "limit" else 1
        tail = f"os.write(1, b'x' * {size})\nsignal.pause()\n"
        client = _archive_client(holder(tail), grandchild, monkeypatch)
        archive = client.image.save(
            "sample", timeout=0.5 if stop == "deadline" else None
        )

        # When a deadline expires, the scope is cancelled, or the archive
        # overflows the scope's stream budget.
        with (
            pytest.raises(_STOPS[stop]),
            observation(
                policy,
                deadline=monotonic() + (0.5 if stop == "scope" else 30),
                cancel=cancel,
            ),
        ):
            if stop != "limit":
                assert next(archive) == b"x"
            if stop == "cancel":
                cancel.set()
            list(archive)

        # Then the grandchild died with the CLI's process group.
        grandchild.started()
        grandchild.assert_gone()


@pytest.mark.parametrize("stop", ["deadline", "scope"])
def test_load_stop_kills_the_cli_process_group(
    stop: Literal["deadline", "scope"], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_archive_stderr_bytes=1024,
    )
    with Grandchild() as grandchild:
        # Given a load with piped stdio whose CLI's grandchild holds its output.
        client = _archive_client(holder("signal.pause()\n"), grandchild, monkeypatch)

        # When the command or scope deadline expires.
        with (
            pytest.raises(_STOPS[stop]),
            observation(
                policy, deadline=monotonic() + (0.5 if stop == "scope" else 30)
            ),
        ):
            client.image.load(b"input", timeout=0.5 if stop == "deadline" else None)

        # Then the grandchild died with the CLI's process group.
        grandchild.started()
        grandchild.assert_gone()


@pytest.mark.parametrize("ending", ["close", "end"])
def test_save_closure_kills_the_group_but_completion_signals_nothing(
    ending: Literal["close", "end"], monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        Grandchild() as grandchild,
        open(os.devnull, "rb") as null,
        stdin_from(null.fileno()),
    ):
        # Given an unbounded save whose CLI's grandchild holds its output, or
        # lets it reach EOF when the CLI exits.
        code = (
            holder("os.write(1, b'x'*65536)\nsignal.pause()\n")
            if ending == "close"
            else holder("os.write(1, b'x\\n')\n", holds_output=False)
        )
        archive = _archive_client(code, grandchild, monkeypatch).image.save("sample")

        # When the caller closes the archive after one block, or exhausts it.
        assert next(archive) == (b"x" * 65536 if ending == "close" else b"x\n")
        if ending == "close":
            assert isinstance(archive, Generator)
            archive.close()
        else:
            assert list(archive) == []

        # Then closure killed the CLI's group, while completion signalled nothing.
        grandchild.started()
        if ending == "close":
            grandchild.assert_gone()
        else:
            grandchild.assert_alive()
