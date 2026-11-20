"""Stand-in CLIs whose forked grandchild proves process-group termination.

A launch built from ``holder()`` forks a grandchild before its own work. Only
the grandchild keeps the status pipe open, so a test observes its death as EOF
on that pipe instead of polling for a process or sleeping.
"""

import os
import select
from collections.abc import Generator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

BOUNDED_WAIT = 5.0


def holder(tail: str, *, holds_output: bool = True) -> str:
    """Return stand-in CLI code that forks a grandchild and then runs ``tail``.

    The code reads the release and status descriptors from ``sys.argv[1:3]``.
    The grandchild writes ``R`` to status, waits for a byte or EOF on release,
    then writes ``D`` and exits. The CLI runs ``tail``, with ``os``,
    ``signal`` and ``sys`` imported, only after the grandchild wrote ``R``
    and the CLI closed its own copies of both descriptors.

    Args:
        tail: Code the CLI runs afterwards, such as ``"signal.pause()\\n"``.
        holds_output: Whether the grandchild keeps the CLI's stdout and stderr.

    Returns:
        Source for ``python -c``.
    """
    return (
        "import os, signal, sys\n"
        "release, status = map(int, sys.argv[1:3])\n"
        "ready, started = os.pipe()\n"
        "if os.fork() == 0:\n"
        + ("" if holds_output else "    os.close(1)\n    os.close(2)\n")
        + "    os.write(status, b'R')\n"
        "    os.write(started, b'R')\n"
        "    os.read(release, 1)\n"
        "    os.write(status, b'D')\n"
        "    os._exit(0)\n"
        "os.read(ready, 1)\n"
        "os.close(release)\n"
        "os.close(status)\n" + tail
    )


class Grandchild:
    """Release and status pipes shared with ``holder()`` grandchildren.

    Append ``argv`` to the launch after the code and pass ``fds`` as its
    ``pass_fds``. Leaving the context releases grandchildren that survived and
    closes every descriptor.
    """

    def __init__(self) -> None:
        release, self._release = os.pipe()
        self._status, status = os.pipe()
        self.fds = (release, status)
        self.argv = (str(release), str(status))
        self._launch_ends_open = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close_launch_ends()
        os.close(self._release)
        os.close(self._status)

    def _close_launch_ends(self) -> None:
        """Close this process's copies of the descriptors launches inherited."""
        if self._launch_ends_open:
            self._launch_ends_open = False
            for fd in self.fds:
                os.close(fd)

    def _report(self) -> bytes:
        """Return the next status byte, or ``b""`` once every grandchild died."""
        ready, _, _ = select.select([self._status], [], [], BOUNDED_WAIT)
        assert ready, "a grandchild neither reported nor exited"
        return os.read(self._status, 1)

    def started(self, count: int = 1) -> None:
        """Wait until ``count`` grandchildren have reported ``R``.

        Args:
            count: Grandchildren expected to have started.
        """
        for _ in range(count):
            assert self._report() == b"R"

    def assert_gone(self) -> None:
        """Assert every grandchild died: the status pipe reaches EOF."""
        self._close_launch_ends()
        assert self._report() == b""

    def assert_alive(self) -> None:
        """Release a single grandchild and assert it answers before exiting."""
        self._close_launch_ends()
        os.write(self._release, b"x")
        assert self._report() == b"D"
        assert self._report() == b""


@contextmanager
def stdin_from(fd: int) -> Generator[None]:
    """Make ``fd`` this process's descriptor 0, which launches inherit, for a while.

    Args:
        fd: Descriptor to install as standard input until the block exits.

    Yields:
        Nothing; the previous descriptor 0 is restored on exit.
    """
    saved = os.dup(0)
    try:
        os.dup2(fd, 0)
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
