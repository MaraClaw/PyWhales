"""Process-backed output iteration, deadlines, and local CLI cleanup.

Reader threads, a child-reaping thread, and a watchdog keep running independently
of stream consumption. On POSIX they wait on readiness: no timer runs while idle,
and every stop path wakes the readers through the stop latch. Cleanup controls
only the local process, not daemon-managed resources.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import suppress
from functools import partial
from subprocess import Popen
from threading import Event, Thread
from time import monotonic
from types import TracebackType
from typing import IO, TYPE_CHECKING, Self, override

from pywhales._readiness import (
    CANCEL_CHECK,
    WakeLatch,
    bounded_wait,
    reap,
    unwind_launch,
    wait_ready,
)
from pywhales._stream_buffer import StreamBuffer
from pywhales.exceptions import (
    DockerException,
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
    get_docker_exception_type,
)
from pywhales.telemetry import Outcome, StopReason
from pywhales.telemetry._lifecycle import ExecutionTelemetry, outcome_of, stop_reason_of

if TYPE_CHECKING:
    from pywhales._observation import ObservationState, _ChildReservation


class ProcessStream(Iterator[tuple[str, bytes]]):
    """Owned CLI process with output readers and an independent watchdog.

    Iteration blocks for stdout/stderr records and drains output after natural
    child exit. A launch-based deadline keeps advancing even when iteration
    pauses or never starts. Use a context manager or call close() when stopping
    early; a bare loop break does not close the stream. Closing kills a running
    local CLI and waits for cleanup, never removing daemon-managed resources.
    Recorded terminal errors are delivered once through iteration or clean
    context exit, without replacing an exception from the context body.

    A stream launched inside an observation keeps that scope's deadline and
    cancel tokens after the scope exits. When one trips first, the local CLI is
    killed and reaped and iteration raises ``ObservationTimeout`` or
    ``ObservationCancelled``. A command deadline wins only when it is strictly
    earlier than the scope's, and a cancel token wins over both.

    The snapshot also carries the scope's stream accounts, whether or not it has
    a deadline or token. Each pipe read charges its stdout or stderr bytes to
    every enclosing account; the first read past a limit stops the stream like a
    scope stop, killing and reaping the local CLI, and iteration raises
    ``ObservationLimitError`` (reason ``total_stream``) once. The scope's
    aggregate I/O accounts are charged by the same reads, so a retained scope
    with only that budget still stops the stream with reason ``total_io``; a
    stream budget wins a same-read tie, and a read never starts on an account
    another operation already overflowed.

    Attributes:
        stdin: Writable child input pipe when launch requested one; otherwise
            ``None``. Cleanup closes this pipe along with the owned process.
    """

    def __init__(
        self,
        process: Popen[bytes],
        full_cmd: list[str],
        *,
        deadline: float | None = None,
        buffer_bytes: int | None = None,
        raw_diagnostics: bool = False,
        strict_workload_errors: bool = False,
        telemetry: ExecutionTelemetry | None = None,
        observation: ObservationState | None = None,
        reservation: _ChildReservation | None = None,
    ) -> None:
        """Take ownership of a launched process and start readers and a watchdog.

        Args:
            process: Running child with stdout and stderr pipes already open.
            full_cmd: Launched arguments retained for exception attributes.
            deadline: Absolute monotonic deadline in seconds, or ``None`` for no
                deadline. The caller computes it from launch time.
            buffer_bytes: Positive byte budget, or ``None`` for unbounded output.
                Queued payload, each unfinished record, and stderr history are
                bounded separately; this is not a total-memory cap.
            raw_diagnostics: Whether exception text may expose command and
                captured-output values. Raw exception attributes remain available
                regardless and must be treated as confidential.
            strict_workload_errors: Keep this workload's failures generic.
            telemetry: Optional launch state completed by consumption or closure.
            observation: Scope snapshotted at launch, or ``None`` outside one. Its
                deadline, cancel tokens, and stream accounts are enforced for the
                stream's whole life, whether or not the scope is still active.
            reservation: Child capacity released at reap, before output drains.
        """
        self._process = process
        self._reservation = reservation
        self._full_cmd = full_cmd
        self.stdin = process.stdin
        self._deadline = deadline
        self._buffer_bytes = buffer_bytes
        self._raw_diagnostics = raw_diagnostics
        self._strict_workload_errors = strict_workload_errors
        self._telemetry = telemetry
        self._telemetry_outcome: Outcome | None = None
        self._telemetry_stop_reason: StopReason | None = None
        self._observation = (
            observation
            if observation is not None and observation.binds_launches
            else None
        )
        self._scope_error: (
            ObservationCancelled | ObservationTimeout | ObservationLimitError | None
        ) = None
        self._read_bytes = {"stdout": 0, "stderr": 0}
        self._queue = StreamBuffer(buffer_bytes)
        self._released = Event()
        self._closed = False
        self._expired = False
        self._exited = False
        self._error: OSError | BufferError | DockerException | None = None
        self._failure_callback: (
            Callable[[OSError | BufferError | DockerException], None] | None
        ) = None
        self._timeout_stdout: bytes | None = None
        self._remaining_readers = 2
        self._live_readers = 2
        self._stderr = bytearray()
        self._pending = {"stdout": bytearray(), "stderr": bytearray()}
        self._pending_bytes = {"stdout": 0, "stderr": 0}
        self._inflight = {"stdout": b"", "stderr": b""}
        assert process.stdout is not None
        assert process.stderr is not None
        stop: WakeLatch | None = None
        started: list[Thread] = []
        try:
            stop = self._stop = WakeLatch()
            self._threads = tuple(
                Thread(target=self._read_pipe, args=(pipe, name), daemon=True)
                for pipe, name in (
                    (process.stdout, "stdout"),
                    (process.stderr, "stderr"),
                )
            )
            self._reaper = Thread(
                target=reap, args=(process, self._publish_exit), daemon=True
            )
            self._watchdog = Thread(target=self._monitor, daemon=True)
            for thread in (*self._threads, self._reaper, self._watchdog):
                thread.start()
                started.append(thread)
        except BaseException:
            self._queue.close()
            unwind_launch(process, stop, started)
            if reservation is not None:
                reservation.release()
            raise

    def _publish_exit(self) -> None:
        """Record child exit and wake the watchdog and any waiting consumer."""
        if self._reservation is not None:
            self._reservation.release()
        with self._queue.condition:
            self._exited = True
            self._queue.condition.notify_all()

    def _on_failure(
        self, callback: Callable[[OSError | BufferError | DockerException], None]
    ) -> None:
        """Register an owner callback, replaying any already recorded failure.

        Notification does not join reader threads and may happen on a worker
        thread. A newly observed nonzero child exit can also trigger it.

        Args:
            callback: Failure notification that must not wait for reader cleanup.
        """
        with self._queue.condition:
            error = self._error
            if error is None:
                self._failure_callback = callback
        if error is not None:
            callback(error)
        else:
            self._notify_exit_failure()

    def _fail(self, error: OSError | BufferError | DockerException) -> None:
        """Record the first failure, wake blocked workers, and notify the owner.

        Args:
            error: Terminal error retained for later delivery to the consumer.
        """
        with self._queue.condition:
            if self._stop.is_set():
                return
            self._error = error.with_traceback(None)
            callback = self._failure_callback
            self._failure_callback = None
            self._stop.set()
            self._queue.close()
        if callback is not None:
            callback(error)

    def _notify_exit_failure(self) -> None:
        """Notify a registered owner if the child has exited unsuccessfully."""
        with self._queue.condition:
            exit_code = self._process.returncode if self._exited else None
            if exit_code is None or exit_code == 0 or self._failure_callback is None:
                return
            stderr = bytes(self._stderr)
        self._fail(
            (
                DockerException
                if self._strict_workload_errors
                else get_docker_exception_type(stderr)
            )(
                self._full_cmd,
                exit_code,
                stderr=stderr,
                raw_diagnostics=self._raw_diagnostics,
            )
        )

    def _read_pipe(self, pipe: IO[bytes], name: str) -> None:
        """Read newline-delimited records and publish completion for one pipe.

        Retain a final unterminated record at EOF. Record read or size failures
        outside the payload queue so queue saturation cannot hide them.

        Args:
            pipe: Owned stdout or stderr pipe, closed when this reader finishes.
            name: ``stdout`` or ``stderr`` source label used for accounting.
        """
        pending = self._pending[name]
        chunk = b""
        offset = 0
        try:
            with pipe:
                os.set_blocking(pipe.fileno(), False)
                while not self._stop.is_set():
                    try:
                        with self._queue.condition:
                            if self._stop.is_set():
                                return
                            try:
                                chunk, overflow = self._read(pipe)
                            except ObservationLimitError as error:
                                self._charge_stream(0, error)
                                return
                            if self._telemetry is not None:
                                self._read_bytes[name] += len(chunk)
                            offset = 0
                            if self._charge_stream(len(chunk), overflow):
                                return
                    except BlockingIOError:
                        wait_ready(pipe.fileno(), latch=self._stop)
                        continue
                    if not chunk:
                        if pending:
                            with self._queue.condition:
                                record = bytes(pending)
                                pending.clear()
                                self._pending_bytes[name] = 0
                                self._publish(name, record)
                        break
                    if name == "stderr":
                        with self._queue.condition:
                            self._check_size(
                                len(self._stderr) + len(chunk), "stderr history"
                            )
                            self._stderr.extend(chunk)
                    while offset < len(chunk) and not self._stop.is_set():
                        newline = chunk.find(b"\n", offset)
                        end = len(chunk) if newline < 0 else newline + 1
                        self._check_size(len(pending) + end - offset, "output record")
                        with self._queue.condition:
                            if self._stop.is_set():
                                return
                            pending.extend(chunk[offset:end])
                            offset = end
                            self._pending_bytes[name] = len(pending)
                            self._queue.condition.notify_all()
                            if newline >= 0:
                                record = bytes(pending)
                                pending.clear()
                                self._pending_bytes[name] = 0
                                if not self._publish(name, record):
                                    return
        except (OSError, BufferError) as error:
            self._fail(error)
        finally:
            with self._queue.condition:
                if self._expired and name == "stdout":
                    assert self._timeout_stdout is not None
                    self._timeout_stdout += chunk[offset:]
                pending.clear()
                self._pending_bytes[name] = 0
                self._live_readers -= 1
            self._queue.finish()

    def _read(self, pipe: IO[bytes]) -> tuple[bytes, ObservationLimitError | None]:
        """Read one chunk of a nonblocking pipe; the queue lock must be held.

        The read is cut so a stream budget flags its overflow at the first excess
        byte, and the scope's aggregate accounts allow and charge it in one step.

        Returns:
            The raw bytes, empty at end of file, and the ``total_io`` error this
            read caused, if any.

        Raises:
            ObservationLimitError: An aggregate account was already overflowed;
                nothing was read.
        """
        scope = self._observation
        if scope is None:
            return os.read(pipe.fileno(), 65536), None
        return scope.read_io(
            scope.stream_allowance(65536), partial(os.read, pipe.fileno())
        )

    def _charge_stream(
        self, size: int, overflow: ObservationLimitError | None = None
    ) -> bool:
        """Charge bytes just read from a pipe; the queue lock must be held.

        An overflow is recorded as the scope error and stops the stream like any
        scope stop, so the watchdog kills and reaps the child and delivery raises
        it once. Nothing is raised on the reader thread. A stream budget overflow
        wins over the same read's aggregate one.

        Args:
            size: Raw bytes the pipe read returned; zero at end of file.
            overflow: The aggregate error that read caused, if any.

        Returns:
            Whether the charge overflowed and stopped the stream.
        """
        scope = self._observation
        if scope is None:
            return False
        if size:
            try:
                scope.charge_stream(size)
            except ObservationLimitError as error:
                overflow = error
        if overflow is None:
            return False
        self._scope_error = overflow.with_traceback(None)
        self._stop.set()
        self._queue.close()
        return True

    def _publish(self, name: str, record: bytes) -> bool:
        """Publish a record while tracking bytes waiting for queue capacity.

        Args:
            name: Output source label.
            record: Complete record that already satisfies the record-size bound.

        Returns:
            Whether the record was queued; false if cleanup closed the queue.
        """
        with self._queue.condition:
            self._inflight[name] = record
            try:
                return self._queue.put((name, record))
            finally:
                self._inflight[name] = b""

    def _check_size(self, size: int, category: str) -> None:
        """Enforce an individual record or stderr-history byte limit.

        Args:
            size: Proposed retained byte count, including any newline.
            category: Accounting category included in the failure message.

        Raises:
            BufferError: If the proposed count exceeds a configured budget.
        """
        if self._buffer_bytes is not None and size > self._buffer_bytes:
            raise BufferError(f"{category} exceeds buffer_bytes={self._buffer_bytes}")

    def _expire(self) -> None:
        """Record the first stop of a running stream while the queue lock is held.

        Mirror the captured path's precedence: a set cancel token, then a
        command deadline strictly earlier than the scope's (only while the child
        runs), then the scope deadline. Capture queued and unfinished stdout
        before closing the queue for a command deadline. Natural child exit is
        not converted into a command timeout during post-exit draining, but a
        scope keeps applying until the stream is delivered.
        """
        if self._stop.is_set():
            return
        scope = self._observation
        now = monotonic()
        if scope is not None and any(token.is_set() for token in scope.cancel_tokens):
            self._scope_error = ObservationCancelled()
        elif (
            self._deadline is not None
            and now >= self._deadline
            and not self._exited
            and (
                scope is None
                or scope.deadline is None
                or self._deadline < scope.deadline
            )
        ):
            self._expired = True
            self._timeout_stdout = (
                self._queue.stdout()
                + self._inflight["stdout"]
                + self._pending["stdout"]
            )
        elif scope is not None and scope.deadline is not None and now >= scope.deadline:
            self._scope_error = ObservationTimeout()
        else:
            return
        self._stop.set()
        self._queue.close()

    def _check_scope(self) -> None:
        """Stop the stream now if its observation was cancelled or has expired."""
        if self._observation is not None:
            with self._queue.condition:
                self._expire()

    def _wake_in(self) -> float | None:
        """Return the wait until the next deadline or cancel check, if any.

        A real deadline is the only timer besides the bounded cancel-token check;
        without either, wait for a notification from the queue.
        """
        ends = []
        if self._deadline is not None and not self._exited:
            ends.append(self._deadline)
        scope = self._observation
        if scope is not None and scope.deadline is not None:
            ends.append(scope.deadline)
        wait = bounded_wait(min(ends) - monotonic()) if ends else None
        if scope is not None and scope.cancel_tokens:
            wait = CANCEL_CHECK if wait is None else min(wait, CANCEL_CHECK)
        return wait

    def _monitor(self) -> None:
        """Enforce deadlines and scope, and reap the child independently of iteration.

        Sleep until stop, child exit, or the next deadline or cancel check, never
        on a fixed interval. Kill a running child when stopped, join the child
        reaper, and then, for an observed stream, keep the scope in force until
        the readers finish. Join the readers, close piped stdin, then close the
        stop latch's wake descriptor, which no thread waits on any longer,
        before signalling that owned resources have been released.
        """
        try:
            with self._queue.condition:
                while not self._stop.is_set() and not self._exited:
                    self._expire()
                    if not self._stop.is_set():
                        self._queue.condition.wait(self._wake_in())
                exited = self._exited
            if self._stop.is_set() and not exited:
                with suppress(ProcessLookupError):
                    self._process.kill()
            self._reaper.join()
            self._notify_exit_failure()
            if self._observation is not None:
                with self._queue.condition:
                    while not self._stop.is_set() and self._live_readers:
                        self._expire()
                        if not self._stop.is_set():
                            self._queue.condition.wait(self._wake_in())
            for thread in self._threads:
                thread.join()
            if self.stdin is not None and not self.stdin.closed:
                os.set_blocking(self.stdin.fileno(), False)
                with suppress(BrokenPipeError, BlockingIOError):
                    self.stdin.close()
            self._stop.close()
        finally:
            self._released.set()

    def _stop_outcome(self) -> tuple[Outcome | None, StopReason | None]:
        """Return the outcome and reason of the error delivery would raise next.

        The one rule behind terminal delivery and ``close()``. It follows the
        order ``_raise_error`` delivers in: a launch deadline, then a scope stop,
        then a reader or budget error. The queue lock must be held.
        """
        if self._expired:
            return "timeout", "command_timeout"
        if self._scope_error is not None:
            return outcome_of(type(self._scope_error)), stop_reason_of(
                self._scope_error
            )
        if self._error is not None:
            return outcome_of(type(self._error)), stop_reason_of(self._error)
        return None, None

    def _raise_error(self) -> None:
        """Wait for cleanup and deliver a recorded terminal error at most once.

        Raises:
            DockerTimeout: If the launch deadline expired while the child ran.
            ObservationCancelled: If an observation token was set.
            ObservationTimeout: If the observation deadline expired.
            ObservationLimitError: If a scope stream budget was exceeded.
            DockerException: If a subprocess failure was recorded.
            BufferError: If retained output exceeded an individual byte limit.
            OSError: If a pipe reader recorded an operating-system failure.
        """
        self._watchdog.join()
        with self._queue.condition:
            if self._telemetry_outcome is None:
                self._telemetry_outcome, self._telemetry_stop_reason = (
                    self._stop_outcome()
                )
            error = self._error
            self._error = None
            expired = self._expired
            self._expired = False
            scope_error = self._scope_error
            self._scope_error = None
        self._complete_telemetry()
        if expired:
            raise DockerTimeout(
                self._full_cmd,
                self._process.returncode,
                self._timeout_stdout,
                bytes(self._stderr),
                raw_diagnostics=self._raw_diagnostics,
            )
        if scope_error is not None:
            raise scope_error
        if error is not None:
            raise error

    def _complete_telemetry(self) -> None:
        """Publish the selected outcome after cleanup, outside stream locks."""
        if self._telemetry is not None and self._telemetry_outcome is not None:
            self._telemetry.stdout_bytes = self._read_bytes["stdout"]
            self._telemetry.stderr_bytes = self._read_bytes["stderr"]
            self._telemetry.return_code = self._process.returncode
            self._telemetry.finish(
                self._telemetry_outcome, stop_reason=self._telemetry_stop_reason
            )

    @override
    def __iter__(self) -> Self:
        """Return this owned output iterator."""
        return self

    @override
    def __next__(self) -> tuple[str, bytes]:
        """Wait for the next output record or deliver terminal status.

        Returns:
            A ``(source, payload)`` pair labelled ``stdout`` or ``stderr``.
            Records include their newline, except an unterminated final record.

        Raises:
            StopIteration: After successful exhaustion or explicit closure with
                no remaining recorded failure.
            DockerException: If the child exits unsuccessfully.
            DockerTimeout: If the running child exceeded its launch deadline.
            ObservationCancelled: If the launch-time observation was cancelled.
            ObservationTimeout: If the launch-time observation deadline expired.
            ObservationLimitError: If the launch-time observation's stream budget
                was exceeded, with reason ``total_stream``.
            BufferError: If a record or retained stderr exceeded its byte limit.
            OSError: If a pipe read failed.
        """
        while True:
            self._check_scope()
            if self._stop.is_set():
                self._raise_error()
                raise StopIteration
            item = self._queue.get()
            if self._stop.is_set():
                self._raise_error()
                raise StopIteration
            if item is not None:
                return item
            self._remaining_readers -= 1
            if not self._remaining_readers:
                break
        self._check_scope()
        self._raise_error()
        with self._queue.condition:
            if self._stop.is_set():
                raise StopIteration
            exit_code = self._process.returncode
            self._closed = True
            self._stop.set()
            self._queue.close()
            self._telemetry_outcome = "success" if exit_code == 0 else "failure"
        self._complete_telemetry()
        if exit_code != 0:
            stderr = bytes(self._stderr)
            exception_type = (
                DockerException
                if self._strict_workload_errors
                else get_docker_exception_type(stderr)
            )
            raise exception_type(
                self._full_cmd,
                exit_code,
                stderr=stderr,
                raw_diagnostics=self._raw_diagnostics,
            )
        raise StopIteration

    def close(self) -> None:
        """Cancel the local CLI and wait for process and reader cleanup.

        Repeated calls are safe. Discard queued payloads but leave recorded
        errors for one later iteration or clean context-exit delivery. This does
        not cancel containers, services, or other daemon-side work.
        """
        with self._queue.condition:
            self._expire()
            if self._telemetry_outcome is None:
                outcome, self._telemetry_stop_reason = self._stop_outcome()
                self._telemetry_outcome = outcome or "closed"
            self._closed = True
            self._stop.set()
            self._queue.close()
        self._watchdog.join()
        self._complete_telemetry()

    def __enter__(self) -> Self:
        """Return the already-running stream without launching another process."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the stream and surface recorded errors only on clean exit.

        Args:
            exc_type: Active context-body exception type, or ``None``.
            exc_value: Active context-body exception, preserved by this method.
            traceback: Context-body traceback supplied by the context protocol.
        """
        self.close()
        if exc_type is None:
            self._raise_error()
