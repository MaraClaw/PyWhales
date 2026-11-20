"""Readiness waits for owned subprocess pipes and stops of whole launches.

On POSIX an idle wait blocks in ``poll()`` on the pipe and a wake descriptor, so
it costs no timer and ends the moment data, space, or a stop signal arrives.
Windows pipes cannot be polled, so waits there are bounded slices.

A POSIX launch whose standard descriptors face no terminal runs in its own
process group, so a library-initiated stop SIGKILLs the CLI together with the
plugins and helpers it spawned. Terminal-facing launches and Windows keep the
leader-only ``Popen.kill()``.
"""

import atexit
import math
import os
import select
import signal
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


class _Group:
    """Process group of one isolated launch whose leader is not yet reaped.

    ``lock`` serializes signalling with reaping, so the group is never signalled
    after its leader was reaped and the ID could have been recycled.
    ``status`` holds the leader's exit status once observed without reaping.
    """

    __slots__ = ("lock", "pgid", "status")

    def __init__(self, pgid: int) -> None:
        self.lock = Lock()
        self.pgid = pgid
        self.status: int | None = None


_groups: Final[dict[Popen[bytes], _Group]] = {}
"""Isolated launches whose leaders the library has not reaped yet."""


def isolates(*inherited: int, tty: bool = False) -> bool:
    """Decide whether a launch runs in its own process group.

    Args:
        inherited: Standard descriptors (0, 1 or 2) the child inherits instead of
            receiving a pipe, the null device, or a file of its own.
        tty: Whether the caller routes output to the terminal.

    Returns:
        ``True`` on POSIX when ``tty`` is false and no inherited descriptor is a
        terminal; such a launch keeps no controlling terminal, so it cannot be
        stopped by terminal job control or lose terminal input.
    """
    return os.name != "nt" and not tty and not any(map(os.isatty, inherited))


def track(process: Popen[bytes], isolated: bool) -> None:
    """Record the group ID of a launch started with ``start_new_session``.

    A launch that did not get its own group stays untracked, so every stop
    falls back to the leader-only kill and never targets Python's own group.

    Args:
        process: Child that was just started.
        isolated: Result of ``isolates()`` passed to the launch.
    """
    if not isolated:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        # A leader that already exited still owns the group it created.
        pgid = process.pid
    if pgid == process.pid and pgid != os.getpgrp():
        _groups[process] = _Group(pgid)


def _signal_group(process: Popen[bytes], group: _Group) -> None:
    """SIGKILL an unreaped leader and its group; call with the group lock held."""
    if process.returncode is not None:
        return
    if group.pgid != os.getpgrp():
        # ESRCH or EPERM (a group of only a zombie leader) means nothing is left.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(group.pgid, signal.SIGKILL)
    with suppress(ProcessLookupError):
        os.kill(process.pid, signal.SIGKILL)


def kill_launch(process: Popen[bytes]) -> None:
    """SIGKILL a launch the library stops.

    An isolated launch loses its whole process group while the leader is
    unreaped; afterwards the call does nothing. Other launches get
    ``Popen.kill()``, which reaches only the leader.

    Args:
        process: Leader of the launch to stop.
    """
    group = _groups.get(process)
    if group is None:
        with suppress(ProcessLookupError):
            process.kill()
        return
    with group.lock:
        _signal_group(process, group)


def await_exit(process: Popen[bytes]) -> int:
    """Wait without a timer for the leader to exit and return its status.

    An isolated leader is left unreaped, so its group stays safe to signal
    until ``reap_launch()``; other leaders are reaped by ``Popen.wait()``.

    Args:
        process: Leader to wait for.

    Returns:
        The exit code, or the negated signal number that ended the leader.
    """
    group = _groups.get(process)
    if group is None or process.returncode is not None:
        return process.wait()
    if group.status is None:
        try:
            info = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            # Another thread reaped it meanwhile and records the status.
            return process.wait()
        assert info is not None
        group.status = (
            info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
        )
    return group.status


def leader_status(process: Popen[bytes]) -> int | None:
    """Return the leader's observed exit status, reaped or not, or ``None``."""
    group = _groups.get(process)
    if group is not None and process.returncode is None:
        return group.status
    return None if process.returncode is None else process.wait()


def reap_launch(process: Popen[bytes]) -> int:
    """Reap the leader once the library stops waiting on its launch.

    Stops must signal before this call: once reaped, an isolated group is
    never signalled again. Repeated calls return the same status.

    Args:
        process: Leader whose launch ended or was stopped.

    Returns:
        The leader's exit code, or the negated signal number that ended it.
    """
    await_exit(process)
    group = _groups.get(process)
    if group is None:
        return process.wait()
    with group.lock:
        code = process.wait()
        _groups.pop(process, None)
    return code


def watch_leader(process: Popen[bytes], on_exit: Callable[[], None]) -> None:
    """Thread target: wait for the leader without a timer, then publish its exit.

    An isolated leader stays unreaped, so a later stop still reaches its group.
    """
    try:
        await_exit(process)
    finally:
        on_exit()


def _kill_live_groups() -> None:
    """Interpreter-exit hook: SIGKILL isolated groups whose leaders are unreaped.

    Streams that were never closed keep their CLI running past interpreter
    exit otherwise. The hook takes no stream or owner lock and joins no thread;
    a group whose lock another thread holds is mid-stop or mid-reap and skipped.
    """
    for process, group in list(_groups.items()):
        if group.lock.acquire(blocking=False):
            try:
                _signal_group(process, group)
            finally:
                group.lock.release()


if os.name != "nt":
    atexit.register(_kill_live_groups)
    # A forked interpreter must never signal groups its parent launched.
    os.register_at_fork(after_in_child=_groups.clear)


def unwind_launch(
    process: Popen[bytes], stop: WakeLatch | None, started: list[Thread]
) -> None:
    """Undo a launch whose setup failed: stop workers, reap the child, close pipes.

    The launch is killed through ``kill_launch()``, so an isolated group dies
    with its leader before the leader is reaped.

    Args:
        process: Child whose pipes and lifetime the failed setup owned.
        stop: Wake latch the started workers wait on, if it was created.
        started: Workers that did start; each is joined before pipes close.
    """
    if stop is not None:
        stop.set()
    kill_launch(process)
    for thread in started:
        thread.join()
    reap_launch(process)
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
    """Watch a child's exit on a helper thread and yield a latch set once it exits.

    An isolated leader is detected without being reaped; the caller reaps it
    with ``reap_launch()`` after the block. Leaving the block through an
    exception, or while the leader still runs, stops the launch through
    ``kill_launch()``. A normal exit after the leader exited signals nothing.
    The helper is joined and only then is the latch closed, so the block's
    waits can rely on the exit signal.

    Args:
        process: Child to watch.
        on_exit: Called on the helper thread once the leader exits.

    Yields:
        The latch set once the leader exits.
    """
    exited = WakeLatch()

    def publish() -> None:
        if on_exit is not None:
            on_exit()
        exited.set()

    try:
        reaper = Thread(target=watch_leader, args=(process, publish), daemon=True)
        reaper.start()
        try:
            yield exited
        except BaseException:
            kill_launch(process)
            raise
        finally:
            if not exited.is_set():
                kill_launch(process)
            reaper.join()
    finally:
        exited.close()
