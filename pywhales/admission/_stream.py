"""Source-context ownership with independently coordinated stream cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from threading import Condition
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self, override

from pywhales._reclaim import watch

from ._guard import operation_scope
from ._state import Outcome

if TYPE_CHECKING:
    from ._request import AdmissionRequest


class _AdmittedCore[T]:
    """Internal state of one `AdmittedStream`, kept apart from its handle.

    Only the handle and the shared reclaimer reference this core. Reclamation
    runs ``close()`` on the reclaimer thread, never in a finalizer, so permit
    release still follows source exit and any in-flight read.
    """

    def __init__(
        self,
        request: AdmissionRequest,
        source: AbstractContextManager[Iterator[T]],
        iterator: Iterator[T],
    ) -> None:
        """Take ownership of an already-entered source and its active request."""
        self._request = request
        self._source = source
        self._iterator = iterator
        self._condition = Condition()
        self._reading = False
        self._closing = False
        self._closed = False
        self._read_error: BaseException | None = None
        self._cleanup_error: BaseException | None = None

    def next(self) -> T:
        """Read one source value for the handle, finishing on its end or failure."""
        with self._condition:
            if self._closing or self._closed:
                raise StopIteration
            if self._reading:
                raise RuntimeError("admitted streams support one consumer")
            self._reading = True
        try:
            with operation_scope(
                id(self._request._owner), self._request._ticket.admission_id
            ):
                value = next(self._iterator)
        except StopIteration:
            self._end_read(None)
            self._finish(None, Outcome.COMPLETED)
            raise
        except BaseException as original:
            self._end_read(original)
            self._finish(original, Outcome.FAILED)
            raise
        else:
            self._end_read(None)
            with self._condition:
                if self._closing or self._closed:
                    raise StopIteration
            return value

    def _end_read(self, error: BaseException | None) -> None:
        with self._condition:
            self._read_error = error
            self._reading = False
            self._condition.notify_all()

    def close(self) -> None:
        """Exit the source cleanly, then release after any in-flight read."""
        self._finish(None, Outcome.STREAM_CLOSED)

    def _finish(self, original: BaseException | None, outcome: Outcome) -> None:
        with self._condition:
            if self._closed:
                if original is not None and original is self._read_error:
                    self._raise_cleanup(original)
                return
            if self._closing:
                interrupted: BaseException | None = None
                while True:
                    try:
                        self._condition.wait_for(lambda: self._closed)
                        break
                    except BaseException as error:
                        interrupted = _combine_failures(interrupted, error)
                if original is not None or interrupted is not None:
                    self._raise_cleanup(original, interrupted)
                return
            self._request._owner._begin_close(self._request)
            self._closing = True
        cleanup_error: BaseException | None = None
        try:
            with operation_scope(
                id(self._request._owner), self._request._ticket.admission_id
            ):
                if original is None:
                    self._source.__exit__(None, None, None)
                else:
                    self._source.__exit__(
                        type(original), original, original.__traceback__
                    )
        except BaseException as error:
            if error is not original:
                cleanup_error = error
        finally:
            with self._condition:
                while self._reading:
                    try:
                        self._condition.wait()
                    except BaseException as interrupted:
                        cleanup_error = _combine_failures(cleanup_error, interrupted)
                failed = (
                    original is not None
                    or self._read_error is not None
                    or cleanup_error is not None
                )
            self._request._owner._release(
                self._request, Outcome.FAILED if failed else outcome
            )
            with self._condition:
                self._cleanup_error = cleanup_error
                self._closed = True
                self._condition.notify_all()
        self._raise_cleanup(original if original is not None else self._read_error)

    def _raise_cleanup(
        self, original: BaseException | None, interrupted: BaseException | None = None
    ) -> None:
        cleanup = self._cleanup_error
        if interrupted is not None:
            cleanup = _combine_failures(
                None if cleanup is original else cleanup, interrupted
            )
        if cleanup is None or cleanup is original:
            return
        if original is not None:
            raise BaseExceptionGroup(
                "stream operation and cleanup failed", [original, cleanup]
            )
        raise cleanup


class AdmittedStream[T](AbstractContextManager["AdmittedStream[T]"], Iterator[T]):
    """One-consumer stream holding one permit through source-context cleanup.

    Exhaustion closes cleanly; a bare loop break retains admission while the
    stream is reachable. A stream dropped without closing is closed by a shared
    background reclaimer once it is garbage collected, through the same
    source-exit-then-release path; a cleanup failure there is logged by type
    only. Explicit close is idempotent, and concurrent closers wait for the sole
    cleanup owner. Source exit runs before waiting for in-flight iteration so it
    can wake blocked reads. There is no idle failure watcher: unobserved source
    failures retain admission until the consumer observes them, closes, or
    drops the stream. Source factory and failed entry must clean up their own
    partial resources.
    """

    def __init__(
        self,
        request: AdmissionRequest,
        source: AbstractContextManager[Iterator[T]],
        iterator: Iterator[T],
    ) -> None:
        """Take ownership of an already-entered source and its active request."""
        self._core = _AdmittedCore(request, source, iterator)
        watch(self, self._core)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Read internal state from the core for white-box inspection.

        Assign or patch internal state on ``_core``; this only forwards reads.

        Raises:
            AttributeError: If the core is unset or lacks the attribute.
        """
        if name == "_core" or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._core, name)

    @override
    def __enter__(self) -> Self:
        """Return the stream whose source context was entered during admission."""
        return self

    @override
    def __iter__(self) -> Self:
        return self

    @override
    def __next__(self) -> T:
        return self._core.next()

    def close(self) -> None:
        """Exit cleanly and release after any in-flight read has unwound.

        A source terminal error raised by clean exit propagates. This closes local
        source resources only and makes no remote cancellation or rollback claim.
        """
        self._core.close()

    def try_resize(self, demand: int) -> bool:
        """Try to replace the active reservation without waiting for capacity.

        Use total positive integer demand, not a delta. Shrinking wakes FIFO
        waiters; growth returns False without change if capacity is unavailable
        or this class has pending work. Equal demand succeeds. Both this handle
        and the original request reject resizing once cleanup begins.

        Raises:
            ValueError: If demand is invalid or exceeds the class capacity.
            RuntimeError: If cleanup has begun or the reservation was released.
        """
        return self._core._request.try_resize(demand)

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Preserve body failures even when the source suppresses them.

        Independent cleanup failure is grouped after the original exception in a
        BaseExceptionGroup. With no original failure, cleanup failure propagates.
        """
        self._core._finish(exc_value, Outcome.STREAM_CLOSED)
        return False


def _combine_failures(
    previous: BaseException | None, error: BaseException
) -> BaseException:
    if previous is None or previous is error:
        return error
    return BaseExceptionGroup("stream cleanup failed", [previous, error])
