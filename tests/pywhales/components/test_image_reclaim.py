"""Image stream groups that callers drop or leave unclosed.

Real local Python children stand in for image transfers, and their small
group buffer leaves both workers blocked on queue capacity. Waits use events,
joins, or one bounded subprocess timeout; nothing sleeps or uses Docker.
"""

import gc
import signal
import subprocess
import sys
import weakref
from collections.abc import Generator
from threading import Event, current_thread
from unittest.mock import patch

import pytest

from pywhales._process_stream import _ProcessCore
from pywhales.components.image.streams import MultiImageStream, _MultiImageCore
from pywhales.utils import PROJECT_ROOT, MappedProcessStream, stream_stdout_and_stderr

BOUNDED_WAIT = 5.0
CHILD = "import os; os.write(1, b'abc\\n' * 64); input()"


def saturated_group(cores: list[_ProcessCore]) -> MultiImageStream:
    """Return a started two-child group whose workers both wait to enqueue."""

    def start(name: str) -> MappedProcessStream[tuple[str, bytes]]:
        child = stream_stdout_and_stderr([sys.executable, "-c", CHILD], pipe_stdin=True)
        cores.append(child._core)
        return MappedProcessStream(child, ((name, line) for _, line in child))

    group = MultiImageStream(["one", "two"], start, buffer_bytes=4)
    core = group._core
    blocked: set[str] = set()
    saturated = Event()
    wait = core._queue.condition.wait

    def wait_and_signal(timeout: float | None = None) -> bool:
        if current_thread() in (core._workers or ()):
            blocked.add(current_thread().name)
            if len(blocked) == 2:
                saturated.set()
        return wait(timeout)

    with patch.object(core._queue.condition, "wait", side_effect=wait_and_signal):
        assert next(group) in {("one", b"abc\n"), ("two", b"abc\n")}
        assert saturated.wait(BOUNDED_WAIT)
    return group


@pytest.fixture
def cores() -> Generator[list[_ProcessCore]]:
    """Yield launched child cores and close any a failing test leaves running."""
    launched: list[_ProcessCore] = []
    yield launched
    for core in launched:
        core.close()


def test_dropped_group_with_blocked_workers_is_reclaimed(
    cores: list[_ProcessCore],
) -> None:
    group = saturated_group(cores)
    core: _MultiImageCore = group._core
    handle = weakref.ref(group)

    del group
    gc.collect()

    assert handle() is None
    assert core._cleanup_done.wait(BOUNDED_WAIT)
    assert core._workers is not None and len(core._workers) == 2
    for worker in core._workers:
        worker.join(BOUNDED_WAIT)
        assert not worker.is_alive()
    assert core._children == set()
    assert len(cores) == 2
    for child in cores:
        assert child._released.wait(BOUNDED_WAIT)
        assert child._process.returncode == -signal.SIGKILL
        assert all(
            pipe.closed
            for pipe in (child.stdin, child._process.stdout, child._process.stderr)
            if pipe is not None
        )


@pytest.mark.parametrize("abandon", [True, False], ids=["abandoned", "reachable"])
def test_unclosed_group_with_blocked_workers_does_not_block_exit(abandon: bool) -> None:
    script = "\n".join(
        (
            "import gc, sys",
            "from threading import enumerate as threads",
            "from tests.pywhales.components.test_image_reclaim import saturated_group",
            "assert not any(t.name == 'pywhales-reclaimer' for t in threads())",
            "group = saturated_group([])",
            "assert any(t.name == 'pywhales-reclaimer' for t in threads())",
            *(("del group", "gc.collect()") if abandon else ()),
            "print('exiting', flush=True)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert (completed.returncode, completed.stdout) == (0, b"exiting\n"), (
        completed.stderr.decode(errors="replace")
    )
