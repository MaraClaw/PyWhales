"""Deadline ownership for direct binary archive subprocesses."""

import os
from collections.abc import Buffer, Iterator
from contextlib import suppress
from io import BufferedReader, RawIOBase
from subprocess import Popen
from threading import Event, Lock, Thread
from time import monotonic
from typing import IO, TYPE_CHECKING, Literal, override

from pywhales._readiness import (
    CANCEL_CHECK,
    bounded_wait,
    reap,
    unwind_launch,
    wait_ready,
)
from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)

if TYPE_CHECKING:
    from pywhales._observation import ObservationState, _ChildReservation

type _Stop = Literal["command", "timeout", "cancelled", "limit"]


class ArchiveDeadline:
    """Account for archive pipe I/O and reap an expired local child.

    Counters include successful writes and reads, never submitted buffer sizes.
    The pipe lock serializes caller I/O with watchdog drains. Reap the process
    and join thread before publishing counters; a deadline is optional.

    Pipe waits are readiness waits made under the pipe lock, so the watchdog
    never closes a pipe beneath one. A wait lasts the remaining time to the
    deadline, which is when the watchdog needs the lock to expire the child,
    and has no timer at all without a deadline or after a natural exit.

    An observation snapshotted at launch adds its own deadline and cancel
    tokens, and keeps them after its ``with`` block exits. The watchdog expires
    the child when the scope ends first, and ``check()`` then raises
    ``ObservationTimeout`` or ``ObservationCancelled`` once the child is
    reaped. Tokens are plain events, so they add only the bounded cancel check
    to a wait. A command deadline reports ``DockerTimeout`` only when strictly
    earlier than the scope's, and a set token wins over both, as in a captured
    run. After a natural exit nothing is left to stop, but ``check()`` still
    reports a scope that ended before the transfer was delivered.

    The snapshot's stream accounts are charged by ``read()`` only, which runs on
    the caller's thread. The first read past a limit kills the child and stops
    the transfer through the same path a cancellation takes, so ``check()``
    raises ``ObservationLimitError`` once the child is reaped. The watchdog's
    drain after a kill and ``record_read()`` only count bytes for telemetry:
    neither charges, so a stderr-selected ``DockerException`` still wins. A
    budget alone adds no timer to a wait.

    The snapshot's stdin accounts are charged by ``write()`` for the bytes each
    pipe write accepted, so partial writes count and competing uploads never pass
    a shared limit. A write that finds an account exhausted while input remains
    stops the transfer like a stream overflow: ``check()`` raises reason
    ``total_stdin`` once the child is reaped. An upload ending exactly at the
    limit succeeds, and the transfer keeps charging after its scope exits.
    """

    def __init__(
        self,
        process: Popen[bytes],
        command: list[str],
        deadline: float | None,
        *,
        raw: bool,
        observation: ObservationState | None = None,
        reservation: _ChildReservation | None = None,
    ) -> None:
        self.process = process
        self._reservation = reservation
        self.command = command
        self.deadline = deadline
        self.raw = raw
        self._scope = (
            observation
            if observation is not None and observation.binds_launches
            else None
        )
        self.bounded = deadline is not None or self._scope is not None
        self._stop: _Stop | None = None
        self._stop_lock = Lock()
        self._limit: ObservationLimitError | None = None
        self.expired = False
        self.stdout: bytes | None = None
        self.stderr: bytes | None = None
        self.stdin_bytes: int | None = None if process.stdin is None else 0
        self.stdout_bytes: int | None = None if process.stdout is None else 0
        self.stderr_bytes: int | None = None if process.stderr is None else 0
        self.released = Event()
        self.lock = Lock()
        self._exited = Event()
        started: list[Thread] = []
        try:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and (
                    self.bounded or (pipe is process.stdin and os.name != "nt")
                ):
                    os.set_blocking(pipe.fileno(), False)
            self._reaper = (
                Thread(target=reap, args=(process, self._publish_exit), daemon=True)
                if deadline is not None
                or (
                    self._scope is not None
                    and (
                        self._scope.deadline is not None
                        or self._scope.cancel_tokens
                        or self._scope.stream_accounts
                        or self._scope.stdin_accounts
                    )
                )
                else None
            )
            self.thread = Thread(target=self._watch, daemon=True)
            if self._reaper is not None:
                self._reaper.start()
                started.append(self._reaper)
            self.thread.start()
            started.append(self.thread)
        except BaseException:
            unwind_launch(process, None, started)
            if reservation is not None:
                reservation.release()
            raise

    def _publish_exit(self) -> None:
        """Release child capacity before publishing reap, without draining pipes."""
        if self._reservation is not None:
            self._reservation.release()
        self._exited.set()

    def _stop_reason(self, *, running: bool) -> _Stop | None:
        """Return why the transfer must stop now, in the captured path's precedence.

        A set cancel token wins, then a command deadline strictly earlier than the
        scope's (only while the child runs), then the scope deadline.
        """
        scope = self._scope
        if scope is not None and any(token.is_set() for token in scope.cancel_tokens):
            return "cancelled"
        now = monotonic()
        if (
            running
            and self.deadline is not None
            and now >= self.deadline
            and (
                scope is None
                or scope.deadline is None
                or self.deadline < scope.deadline
            )
        ):
            return "command"
        if scope is not None and scope.deadline is not None and now >= scope.deadline:
            return "timeout"
        return None

    def _remaining(self, *, running: bool = True) -> float | None:
        """Return seconds to the next deadline or cancel check; None means no timer.

        Once the child has exited, only the observation still applies.
        """
        scope = self._scope
        ends = [
            end
            for end in (
                self.deadline if running else None,
                None if scope is None else scope.deadline,
            )
            if end is not None
        ]
        remaining = bounded_wait(min(ends) - monotonic()) if ends else None
        if scope is not None and scope.cancel_tokens:
            remaining = (
                CANCEL_CHECK if remaining is None else min(remaining, CANCEL_CHECK)
            )
        return remaining

    def _claim(self, reason: _Stop) -> None:
        """Record why the transfer stopped; the first reason is never replaced."""
        with self._stop_lock:
            if self._stop is None:
                self._stop = reason

    def _watch(self) -> None:
        try:
            if self._reaper is None:
                self.process.wait()
                self._publish_exit()
                return
            while not self._exited.wait(self._remaining()):
                reason = self._stop_reason(running=True)
                if reason is not None:
                    self._claim(reason)
                    break
            if self._exited.is_set() and self._stop != "limit":
                return
            self.expired = True
            with suppress(ProcessLookupError):
                self.process.kill()
            self.process.wait()
            with self.lock:
                self.stdout = self._drain(self.process.stdout)
                self.stderr = self._drain(self.process.stderr)
                for pipe in (
                    self.process.stdin,
                    self.process.stdout,
                    self.process.stderr,
                ):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
        finally:
            if self._reaper is not None:
                self._reaper.join()
            self.released.set()

    def _wait(self, pipe: IO[bytes], *, writable: bool) -> bool:
        """Wait for readiness while holding the pipe lock.

        Returns:
            False if a deadline has passed and the watchdog has yet to decide
            between expiry and a natural exit; the caller then releases the
            lock and waits for that decision.
        """
        running = not self.released.is_set()
        timeout = self._remaining(running=running)
        if timeout is not None and timeout <= 0:
            return not running
        wait_ready(pipe.fileno(), writable=writable, timeout=timeout)
        return True

    def _drain(self, pipe: IO[bytes] | None) -> bytes | None:
        if pipe is None or pipe.closed:
            return None
        chunks = []
        while True:
            try:
                chunk = self._read(pipe, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _read(self, pipe: IO[bytes], size: int) -> bytes:
        """Read once under the caller-held lock and account at the pipe boundary."""
        chunk = os.read(pipe.fileno(), size)
        self.record_read(pipe, chunk)
        return chunk

    def record_read(self, pipe: IO[bytes], chunk: bytes) -> None:
        """Account for one pipe-boundary read while holding the lock."""
        if pipe is self.process.stdout:
            assert self.stdout_bytes is not None
            self.stdout_bytes += len(chunk)
        elif pipe is self.process.stderr:
            assert self.stderr_bytes is not None
            self.stderr_bytes += len(chunk)

    def check(self) -> None:
        """Raise if the transfer was stopped by a deadline or the observation.

        Raises:
            DockerTimeout: The command deadline expired and the child was reaped.
            ObservationTimeout: The observation deadline passed first.
            ObservationCancelled: An observation token was set.
            ObservationLimitError: A caller-thread read overflowed a stream
                account (reason ``total_stream``), or a write found a stdin
                account exhausted (reason ``total_stdin``), and the child was
                reaped.
        """
        released = self.released.is_set()
        if self.expired:
            self.released.wait()
            if self._stop == "cancelled":
                raise ObservationCancelled()
            if self._stop == "timeout":
                raise ObservationTimeout()
            if self._stop == "limit":
                assert self._limit is not None
                raise self._limit
            raise DockerTimeout(
                self.command,
                self.process.returncode,
                self.stdout,
                self.stderr,
                raw_diagnostics=self.raw,
            )
        if released and self._scope is not None:
            stop = self._stop_reason(running=False)
            if stop == "cancelled":
                raise ObservationCancelled()
            if stop == "timeout":
                raise ObservationTimeout()

    def _exceed(self, error: ObservationLimitError) -> None:
        """Stop the transfer for an exhausted budget; the lock is held.

        Kills the child and marks the transfer expired, so the next ``check()``
        raises ``error`` after the watchdog reaps and drains, exactly as a scope
        cancellation does. The watchdog is woken by the child's exit, so a budget
        needs no timer.
        """
        self._limit = error.with_traceback(None)
        self._claim("limit")
        self.expired = True
        with suppress(ProcessLookupError):
            self.process.kill()

    def _charge(self, chunk: bytes) -> bool:
        """Charge a caller-thread read to the stream accounts; the lock is held.

        Returns:
            Whether the read overflowed and the chunk must be discarded.
        """
        scope = self._scope
        if scope is None or not chunk:
            return False
        try:
            scope.charge_stream(len(chunk))
        except ObservationLimitError as error:
            self._exceed(error)
            return True
        return False

    def _send(self, pipe: IO[bytes], view: memoryview) -> int:
        """Write once, limited by and charged to the stdin accounts; lock held."""
        descriptor = pipe.fileno()
        if self._scope is None:
            return os.write(descriptor, view)
        return self._scope.write_stdin(
            len(view), lambda allowed: os.write(descriptor, view[:allowed])
        )

    def read(self, pipe: IO[bytes], size: int = 65536) -> bytes:
        while True:
            self.check()
            settle = False
            with self.lock:
                if self.expired:
                    continue
                scope = self._scope
                try:
                    chunk = self._read(
                        pipe, size if scope is None else scope.stream_allowance(size)
                    )
                except BlockingIOError:
                    settle = not self._wait(pipe, writable=False)
                else:
                    if not self._charge(chunk):
                        return chunk
            if settle:
                self.released.wait()

    def lines(self, pipe: IO[bytes]) -> Iterator[bytes]:
        """Yield buffered lines, counting raw reads including read-ahead."""
        with BufferedReader(_ArchiveReader(self, pipe)) as reader:
            yield from reader

    def write(self, pipe: IO[bytes], data: bytes) -> None:
        self.check()
        offset = 0
        view = memoryview(data)
        while offset < len(data):
            self.check()
            settle = False
            with self.lock:
                if self.expired:
                    continue
                try:
                    end = offset + 4096 if os.name == "nt" else len(view)
                    written = self._send(pipe, view[offset:end])
                except BlockingIOError:
                    settle = not self._wait(pipe, writable=True)
                except ObservationLimitError as error:
                    self._exceed(error)
                else:
                    assert self.stdin_bytes is not None
                    self.stdin_bytes += written
                    offset += written
            if settle:
                self.released.wait()


class _ArchiveReader(RawIOBase):
    """Adapt measured reads without taking ownership of the Popen-owned pipe."""

    def __init__(self, owner: ArchiveDeadline, pipe: IO[bytes]) -> None:
        super().__init__()
        self._owner = owner
        self._pipe = pipe

    @override
    def readable(self) -> bool:
        return True

    @override
    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        chunk = self._owner.read(self._pipe, len(view))
        view[: len(chunk)] = chunk
        return len(chunk)
