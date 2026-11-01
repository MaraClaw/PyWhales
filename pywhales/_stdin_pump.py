"""Measured byte input while subprocess communication drains output concurrently."""

import errno
import os
from collections.abc import Buffer, Iterable
from contextlib import suppress
from subprocess import Popen
from threading import Thread
from types import TracebackType
from typing import TYPE_CHECKING, Self

from pywhales._readiness import WakeLatch, wait_ready
from pywhales.telemetry._lifecycle import ExecutionTelemetry

if TYPE_CHECKING:
    from pywhales._observation import ObservationState


class StdinPump:
    """Own a cancellable writer and accumulate only successful local pipe writes.

    Detach stdin while communicate drains output, preventing it from closing the
    writer's pipe. Restore the closed pipe and join the writer before completion.
    No supplied input leaves stdin and its unavailable measurement unchanged. A
    writer facing a full pipe waits for space or the stop latch, never a timer.

    Input is one buffer or an iterable of chunks that the writer thread pulls
    lazily, never buffering the whole iterable. Iterator code is the caller's:
    stopping the pump cannot interrupt it, so joining waits for a chunk in flight.
    A failing iterator, a write error, or an exhausted stdin budget kills the
    child and is raised on a clean exit; an active body exception wins.

    With an ``observation``, every write is limited by and charged to its stdin
    accounts, so bytes the pipe accepted are the only bytes counted.
    """

    def __init__(
        self,
        process: Popen[bytes],
        data: bytes | Iterable[bytes] | None,
        telemetry: ExecutionTelemetry,
        *,
        observation: ObservationState | None = None,
    ) -> None:
        self._process = process
        self._data = data
        self._telemetry = telemetry
        self._observation = observation
        self._pipe = process.stdin
        self._stop: WakeLatch | None = None
        self._thread: Thread | None = None
        self._error: Exception | None = None

    def __enter__(self) -> Self:
        if self._data is None:
            return self
        assert self._pipe is not None
        self._telemetry.stdin_bytes = 0
        stop: WakeLatch | None = None
        try:
            stop = WakeLatch()
            os.set_blocking(self._pipe.fileno(), False)
            thread = Thread(
                target=self._write, args=(stop,), name="pywhales-stdin", daemon=True
            )
            thread.start()
        except OSError, RuntimeError:
            if stop is not None:
                stop.close()
            with suppress(ProcessLookupError):
                self._process.kill()
            raise
        self._stop = stop
        self._thread = thread
        self._process.stdin = None
        return self

    def _send(self, view: memoryview) -> int:
        """Write once, within the stdin budget when one applies."""
        assert self._pipe is not None
        descriptor = self._pipe.fileno()
        if self._observation is None:
            return os.write(descriptor, view)
        return self._observation.write_stdin(
            len(view), lambda allowed: os.write(descriptor, view[:allowed])
        )

    def _send_all(self, stop: WakeLatch) -> None:
        assert self._pipe is not None and self._data is not None
        chunks = iter((self._data,) if isinstance(self._data, Buffer) else self._data)
        sent = 0
        while not stop.is_set() and (chunk := next(chunks, None)) is not None:
            view = memoryview(chunk)
            offset = 0
            while offset < len(view) and not stop.is_set():
                try:
                    written = self._send(view[offset : offset + 4096])
                except BlockingIOError:
                    wait_ready(self._pipe.fileno(), writable=True, latch=stop)
                    continue
                offset += written
                sent += written
                self._telemetry.stdin_bytes = sent

    def _fail(self, error: Exception) -> None:
        """Keep the first failure for the caller and stop the child."""
        self._error = error
        with suppress(ProcessLookupError):
            self._process.kill()

    def _write(self, stop: WakeLatch) -> None:
        assert self._pipe is not None
        try:
            with self._pipe:
                self._send_all(stop)
        except BrokenPipeError:
            # Match communicate: a child may close stdin before all input arrives.
            return
        except OSError as error:
            if os.name == "nt" and error.errno == errno.EINVAL:
                return
            self._fail(error)
        except Exception as error:
            # Budget exhaustion or caller iterator failure ends the writer thread,
            # so it must travel to the thread that owns the pump.
            self._fail(error)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stop is not None:
            self._stop.set()
        try:
            if exc_type is not None:
                with suppress(ProcessLookupError):
                    self._process.kill()
                self._process.wait()
        finally:
            if self._thread is not None:
                self._thread.join()
                self._process.stdin = self._pipe
            if self._stop is not None:
                self._stop.close()
        if exc_type is None and self._error is not None:
            raise self._error
