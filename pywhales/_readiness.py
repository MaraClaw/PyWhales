"""Readiness waits for owned subprocess pipes without fixed polling intervals.

On POSIX an idle wait blocks in ``poll()`` on the pipe and a wake descriptor, so
it costs no timer and ends the moment data, space, or a stop signal arrives.
Windows pipes cannot be polled, so waits there are bounded slices.
"""

import math
import os
import select
import socket
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from subprocess import Popen
from threading import Event, Lock, Thread
from typing import Final, override

POLL_SLICE: Final = 0.01
"""Longest Windows wait, in seconds, before callers re-check their state."""

MAX_WAIT: Final = 86400.0
"""Longest single OS wait; longer deadlines are re-evaluated after each slice."""

CANCEL_CHECK: Final = 0.05
"""Longest wait, in seconds, while an observation cancel token must be polled.

Cancel tokens are plain events that cannot wake a readiness wait, so this is the
only timer a wait may add for them. Deadlines and pipe readiness need no slice.
"""


def bounded_wait(seconds: float | None) -> float | None:
    """Clamp a remaining time to one the OS and lock primitives accept.

    Args:
        seconds: Remaining seconds, or ``None`` to wait without a timer.

    Returns:
        ``None`` for no timer, otherwise a non-negative finite wait.
    """
    return None if seconds is None else min(max(0.0, seconds), MAX_WAIT)


class WakeLatch(Event):
    """One-way event that also makes a descriptor readable once it is set.

    Every stop or exit path calls ``set()``, which wakes any thread blocked in
    ``wait_ready``. ``set()`` and ``close()`` are serialized, so a late ``set()``
    after ``close()`` can never signal a recycled descriptor. Close only after
    every thread waiting on the descriptor has been joined. Never ``clear()``.

    A socket pair carries the signal so wake bytes stay out of ``os.read`` and
    ``os.write`` accounting of the payload pipes. Windows has no descriptor.
    """

    def __init__(self) -> None:
        """Create an unset latch and, on POSIX, its non-inheritable wake pair."""
        super().__init__()
        self._guard = Lock()
        self._sockets = socket.socketpair() if os.name != "nt" else None
        self._signaled = False

    def fileno(self) -> int | None:
        """Return the wake descriptor, or ``None`` on Windows or after close."""
        sockets = self._sockets
        return None if sockets is None else sockets[0].fileno()

    @override
    def set(self) -> None:
        """Set the event and make the wake descriptor readable, once."""
        super().set()
        with self._guard:
            if self._sockets is not None and not self._signaled:
                self._signaled = True
                self._sockets[1].send(b"\0")

    def close(self) -> None:
        """Release the wake descriptor; repeated calls and later ``set()`` are safe."""
        with self._guard:
            sockets, self._sockets = self._sockets, None
        if sockets is not None:
            for end in sockets:
                end.close()


def wait_ready(
    fd: int,
    *,
    writable: bool = False,
    latch: WakeLatch | None = None,
    timeout: float | None = None,
) -> None:
    """Block until a descriptor is ready, a latch is set, or a timeout elapses.

    Callers re-check their own state afterwards: a return only means that
    something may have changed. POSIX uses no timer unless ``timeout`` is
    given; Windows waits at most one short slice.

    Args:
        fd: Pipe descriptor to wait on.
        writable: Wait for space to write instead of data to read.
        latch: Optional stop latch whose ``set()`` ends the wait early.
        timeout: Remaining seconds, or ``None`` to wait without a timer.
    """
    if os.name == "nt":
        slice_ = POLL_SLICE if timeout is None else min(POLL_SLICE, timeout)
        if latch is None:
            time.sleep(slice_)
        else:
            latch.wait(slice_)
        return
    poller = select.poll()
    poller.register(fd, select.POLLOUT if writable else select.POLLIN)
    wake = None if latch is None else latch.fileno()
    if wake is not None:
        poller.register(wake, select.POLLIN)
    limit = bounded_wait(timeout)
    poller.poll(None if limit is None else math.ceil(limit * 1000))


def reap(process: Popen[bytes], on_exit: Callable[[], None]) -> None:
    """Thread target: wait for a child without a timer, then publish its exit."""
    try:
        process.wait()
    finally:
        on_exit()


def unwind_launch(
    process: Popen[bytes], stop: WakeLatch | None, started: list[Thread]
) -> None:
    """Undo a launch whose setup failed: stop workers, reap the child, close pipes.

    Args:
        process: Child whose pipes and lifetime the failed setup owned.
        stop: Wake latch the started workers wait on, if it was created.
        started: Workers that did start; each is joined before pipes close.
    """
    if stop is not None:
        stop.set()
    with suppress(ProcessLookupError):
        process.kill()
    for thread in started:
        thread.join()
    process.wait()
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with suppress(BrokenPipeError):
                pipe.close()
    if stop is not None:
        stop.close()


@contextmanager
def watch_exit(
    process: Popen[bytes], on_exit: Callable[[], None] | None = None
) -> Generator[WakeLatch]:
    """Reap a child on a helper thread and yield a latch set once it exits.

    Leaving the block kills a child that still runs, joins the helper, and only
    then closes the latch, so the block's waits can rely on the exit signal.
    """
    exited = WakeLatch()

    def publish() -> None:
        if on_exit is not None:
            on_exit()
        exited.set()

    try:
        reaper = Thread(target=reap, args=(process, publish), daemon=True)
        reaper.start()
        try:
            yield exited
        finally:
            with suppress(ProcessLookupError):
                process.kill()
            reaper.join()
    finally:
        exited.close()
