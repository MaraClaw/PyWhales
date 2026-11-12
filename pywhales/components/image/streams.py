"""Lazy, owned coordination of concurrent image transfer output."""

import logging
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextvars import Context, copy_context
from multiprocessing.pool import ThreadPool
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Any, Self, override

from pywhales._reclaim import watch
from pywhales._stream_buffer import StreamBuffer
from pywhales.exceptions import StreamBufferLimitError
from pywhales.utils import MappedProcessStream

LOGGER = logging.getLogger(__name__)

WORKERS = 4
"""Most image transfers running at once, for blocking pools and stream groups."""


def _fan_out(context: Context) -> int:
    """Return one to ``WORKERS`` workers; no free slot still starts one to be refused."""
    from pywhales._observation import get_observation

    state = context.run(get_observation)
    allowance = state.child_allowance() if state is not None else None
    return WORKERS if allowance is None else max(1, min(WORKERS, allowance))


def map_in_contexts[T, R](
    function: Callable[[T], R], arguments: Iterable[T]
) -> list[R]:
    """Run ``function`` for each argument on a worker pool, in input order.

    Every call enters its own copy of the caller's context, copied here in the
    calling thread, so workers see the caller's observation scope and execution
    settings like a single transfer does. A context can be entered by only one
    thread at a time, so sharing one across workers would raise ``RuntimeError``.
    The first failure terminates the pool, which drops queued calls; calls that
    already run cannot be interrupted. On success the workers have exited. The pool
    fits the caller's remaining observation child slots, not below one.

    Args:
        function: Task applied to one argument on a worker thread.
        arguments: Task inputs, consumed before any worker starts.

    Returns:
        The results in the order of ``arguments``.
    """
    tasks = [(copy_context(), argument) for argument in arguments]
    workers = _fan_out(copy_context())
    failure_lock = Lock()
    failures: list[BaseException] = []

    def run_one(context: Context, argument: T) -> R:
        with failure_lock:
            if failures:
                raise failures[0]
        try:
            return context.run(function, argument)
        except BaseException as error:
            with failure_lock:
                if not failures:
                    failures.append(error)
            raise

    with ThreadPool(workers) as pool:
        try:
            results = pool.starmap(run_one, tasks)
        except BaseException:
            if failures:
                raise failures[0] from None
            raise
        pool.close()
        pool.join()
    return results


class _MultiImageCore:
    """Internal state of one `MultiImageStream`, shared with its workers.

    Worker threads, child failure callbacks and the shared reclaimer reference
    only this core, never the public handle. Workers are daemon threads, so a
    group left unclosed never delays interpreter exit, even while a worker
    waits for queue capacity or child output.
    """

    def __init__(
        self,
        names: Sequence[str],
        factory: Callable[[str], MappedProcessStream[tuple[str, bytes]]],
        buffer_bytes: int | None,
    ) -> None:
        """Store transfer inputs and snapshot the creation context."""
        self._names = tuple(names)
        self._factory = factory
        self._context = copy_context()
        self._lock = Lock()
        self._buffer_bytes = buffer_bytes
        self._queue = StreamBuffer(buffer_bytes)
        self._workers: tuple[Thread, ...] | None = None
        self._tasks: deque[tuple[Context, str]] = deque()
        self._children: set[MappedProcessStream[tuple[str, bytes]]] = set()
        self._remaining = len(self._names)
        self._stopped = False
        self._closing = False
        self._failure_cleanup_started = False
        self._cleanup_done = Event()
        self._error: BaseException | None = None

    def _start(self) -> None:
        """Queue every transfer and start the sized workers on first use.

        Each transfer gets its own copy of the creation context. Workers are
        recorded as they start, so a failed start closes only the started ones.
        """
        started = False
        try:
            with self._lock:
                if not self._stopped and self._workers is None:
                    count = min(_fan_out(self._context.copy()), len(self._names))
                    self._tasks.extend(
                        (self._context.copy(), name) for name in self._names
                    )
                    self._workers = ()
                    for _ in range(count):
                        worker = Thread(target=self._work, daemon=True)
                        worker.start()
                        self._workers += (worker,)
                    if not self._names:
                        self._queue.finish()
            started = True
        finally:
            if not started:
                self.close()

    def _work(self) -> None:
        """Run queued transfers in their own contexts until none remain."""
        while True:
            with self._lock:
                if not self._tasks:
                    return
                context, name = self._tasks.popleft()
            error: BaseException | None = None
            try:
                context.run(self._consume, name)
            except BaseException as caught:
                error = caught
            try:
                self._worker_done(error)
            except BaseException as cleanup:
                LOGGER.warning(
                    "Image stream worker cleanup raised %s; details withheld",
                    type(cleanup).__qualname__,
                )

    def _cancel_queued(self) -> None:
        """Drop transfers no worker has started; the lock must be held."""
        self._remaining -= len(self._tasks)
        self._tasks.clear()

    def _consume(self, name: str) -> None:
        with self._lock:
            if self._stopped:
                return
            child = self._factory(name)
            self._children.add(child)
        try:
            child._on_failure(self._record_error)
            for item in child:
                if self._buffer_bytes is not None and len(item[1]) > self._buffer_bytes:
                    raise StreamBufferLimitError(
                        f"image stream record exceeds buffer_bytes={self._buffer_bytes}",
                        "output_record",
                    )
                if not self._queue.put(item):
                    break
        finally:
            child.close()
            with self._lock:
                self._children.remove(child)

    def _record_error(self, error: BaseException) -> None:
        with self._lock:
            if self._error is None:
                self._error = error
            self._stopped = True
            self._queue.close()

    def _worker_done(self, error: BaseException | None) -> None:
        """Account for one finished transfer and start failure cleanup once.

        The first transfer to finish after a stop that isn't an explicit close
        drops queued transfers and closes active siblings without waiting for
        the consumer.

        Args:
            error: Exception the transfer raised, or ``None`` on normal return.
        """
        if error is not None:
            self._record_error(error)
        with self._lock:
            self._remaining -= 1
            if (
                self._stopped
                and not self._closing
                and not self._failure_cleanup_started
            ):
                self._failure_cleanup_started = True
                children = tuple(self._children)
                self._cancel_queued()
            else:
                children = ()
            if self._stopped:
                self._queue.close()
            elif not self._remaining:
                self._queue.finish()
        for child in children:
            child.close()

    def next(self) -> tuple[str, bytes]:
        """Start workers if needed, then return a record or deliver the end."""
        self._start()
        while True:
            with self._lock:
                stopped = self._stopped
            if stopped:
                break
            item = self._queue.get()
            with self._lock:
                if self._stopped:
                    break
            if item is None:
                break
            return item
        self.close()
        with self._lock:
            error = self._error
            self._error = None
        if error is not None:
            raise error
        raise StopIteration

    def close(self) -> None:
        """Stop admission, close children, and join the workers once.

        Concurrent callers wait for the one cleanup owner to finish.
        """
        with self._lock:
            self._stopped = True
            self._queue.close()
            cleanup_owner = not self._closing
            self._closing = True
            children = tuple(self._children)
            workers = self._workers or ()
            self._cancel_queued()
        if not cleanup_owner:
            self._cleanup_done.wait()
            return
        try:
            for child in children:
                child.close()
        finally:
            try:
                for worker in workers:
                    worker.join()
            finally:
                self._cleanup_done.set()


class MultiImageStream(Iterator[tuple[str, bytes]]):
    """Lazy image transfer group owning at most four active child streams.

    Workers start on the first `next()` call, not construction or context entry.
    The first call sizes the pool to the creation context's remaining child slots.
    Records contain an image reference and output bytes; interleaving across
    images and stdout/stderr is not globally ordered. Exhaustion waits for worker
    cleanup. Use a context manager or explicit `close()` when stopping early;
    a bare loop break leaves ownership with the caller. A group dropped without
    closing is closed by a shared background reclaimer once it is garbage
    collected. Workers are daemon threads, so an unclosed group doesn't delay
    interpreter exit; its local CLIs are then not reaped.

    Closing cancels local CLI processes, not daemon-managed images. Worker errors
    stop active and queued sibling work without waiting for consumption. The
    first recorded error is raised once by iteration, including after explicit
    close; context exit itself only closes the group.

    The group snapshots the context it is created in, so every worker, whenever
    it starts, runs in its own copy of that context: children launch under the
    creator's observation scope, deadline, and tokens even if iteration begins
    after the ``with`` block that created the group has exited.
    """

    def __init__(
        self,
        names: Sequence[str],
        factory: Callable[[str], MappedProcessStream[tuple[str, bytes]]],
        *,
        buffer_bytes: int | None = None,
    ) -> None:
        """Store transfer inputs without starting workers or child processes.

        Args:
            names: References to schedule in the supplied order; deduplication is
                the caller's responsibility.
            factory: Start and return an owned child stream for one reference.
            buffer_bytes: Queued-payload budget in bytes, or `None` for unbounded
                buffering. A record exceeding the budget raises `StreamBufferLimitError`
                during consumption. Child budgets are supplied by the factory.
        """
        self._core = _MultiImageCore(names, factory, buffer_bytes)
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
    def __iter__(self) -> Self:
        return self

    @override
    def __next__(self) -> tuple[str, bytes]:
        return self._core.next()

    def close(self) -> None:
        """Stop admission, close children, and wait for worker cleanup.

        Repeated calls are safe, including before the first iteration. Queued
        records are discarded and pending worker errors remain available for one
        delivery through iteration. This method does not cancel daemon-side work.
        """
        self._core.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._core.close()


class _EmptyImageStream(Iterator[tuple[str, bytes]]):
    """Launch-free empty progress stream with the owned-stream interface."""

    @override
    def __iter__(self) -> Self:
        return self

    @override
    def __next__(self) -> tuple[str, bytes]:
        raise StopIteration

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


type ImageStream = MappedProcessStream[tuple[str, bytes]] | MultiImageStream
type EmptyImageStream = _EmptyImageStream
