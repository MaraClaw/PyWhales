"""Lazy, owned coordination of concurrent image transfer output."""

from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context, copy_context
from multiprocessing.pool import ThreadPool
from threading import Event, Lock
from types import TracebackType
from typing import Self, override

from pywhales._stream_buffer import StreamBuffer
from pywhales.exceptions import StreamBufferLimitError
from pywhales.utils import MappedProcessStream

WORKERS = 4
"""Most image transfers running at once, for blocking pools and stream groups."""


def map_in_contexts[T, R](
    function: Callable[[T], R], arguments: Iterable[T]
) -> list[R]:
    """Run ``function`` for each argument on a worker pool, in input order.

    Every call enters its own copy of the caller's context, copied here in the
    calling thread, so workers see the caller's observation scope and execution
    settings like a single transfer does. A context can be entered by only one
    thread at a time, so sharing one across workers would raise ``RuntimeError``.
    The first failure terminates the pool, which drops queued calls; calls that
    already run cannot be interrupted. On success the workers have exited.

    Args:
        function: Task applied to one argument on a worker thread.
        arguments: Task inputs, consumed before any worker starts.

    Returns:
        The results in the order of ``arguments``.
    """
    tasks = [(copy_context(), argument) for argument in arguments]

    def run_one(context: Context, argument: T) -> R:
        return context.run(function, argument)

    with ThreadPool(WORKERS) as pool:
        results = pool.starmap(run_one, tasks)
        pool.close()
        pool.join()
    return results


class MultiImageStream(Iterator[tuple[str, bytes]]):
    """Lazy image transfer group owning at most four active child streams.

    Workers start on the first `next()` call, not construction or context entry.
    Records contain an image reference and output bytes; interleaving across
    images and stdout/stderr is not globally ordered. Exhaustion waits for worker
    cleanup. Use a context manager or explicit `close()` when stopping early;
    a bare loop break leaves ownership with the caller.

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
        self._names = tuple(names)
        self._factory = factory
        self._context = copy_context()
        self._lock = Lock()
        self._buffer_bytes = buffer_bytes
        self._queue = StreamBuffer(buffer_bytes)
        self._executor: ThreadPoolExecutor | None = None
        self._children: set[MappedProcessStream[tuple[str, bytes]]] = set()
        self._remaining = len(names)
        self._stopped = False
        self._closing = False
        self._failure_cleanup_started = False
        self._cleanup_done = Event()
        self._error: BaseException | None = None

    def _start(self) -> None:
        started = False
        try:
            with self._lock:
                if not self._stopped and self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=WORKERS)
                    for name in self._names:
                        future = self._executor.submit(
                            self._consume_in(self._context.copy()), name
                        )
                        future.add_done_callback(self._worker_done)
                    if not self._names:
                        self._queue.finish()
            started = True
        finally:
            if not started:
                self.close()

    def _consume_in(self, context: Context) -> Callable[[str], None]:
        """Return a task entering ``context``, which only that task's thread may do."""

        def consume(name: str) -> None:
            context.run(self._consume, name)

        return consume

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

    def _worker_done(self, future: Future[None]) -> None:
        error = None if future.cancelled() else future.exception()
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
                executor = self._executor
            else:
                children = ()
                executor = None
            if self._stopped:
                self._queue.close()
            elif not self._remaining:
                self._queue.finish()
        try:
            for child in children:
                child.close()
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    @override
    def __iter__(self) -> Self:
        return self

    @override
    def __next__(self) -> tuple[str, bytes]:
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
        """Stop admission, close children, and wait for worker cleanup.

        Repeated calls are safe, including before the first iteration. Queued
        records are discarded and pending worker errors remain available for one
        delivery through iteration. This method does not cancel daemon-side work.
        """
        with self._lock:
            self._stopped = True
            self._queue.close()
            cleanup_owner = not self._closing
            self._closing = True
            children = tuple(self._children)
            executor = self._executor
        if not cleanup_owner:
            self._cleanup_done.wait()
            return
        try:
            for child in children:
                child.close()
        finally:
            try:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
            finally:
                self._cleanup_done.set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


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
