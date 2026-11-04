"""Finite drop-newest buffering with explicit application flush ownership."""

from collections import deque
from collections.abc import Mapping
from threading import Lock
from types import MappingProxyType
from typing import Literal, assert_never

from ._records import (
    STOP_REASONS,
    AdmissionDecisionRecord,
    CompletionRecord,
    LaunchRecord,
    RefusalRecord,
    StopReason,
    TelemetryObserver,
    TelemetryRecord,
)


class BoundedExporter:
    """Buffer records without running the sink on the execution thread.

    No worker, timer, network connection, or automatic shutdown is created.
    The application owns calling flush and shutdown, and bounding sink latency.
    Concurrent flushes are serialized. A flush processes a finite snapshot;
    arrivals during export remain queued for a later flush. Sink failures are
    counted, discarded without retry, and never rendered or propagated.
    """

    def __init__(
        self,
        sink: TelemetryObserver,
        *,
        capacity: int,
        drop_policy: Literal["drop_newest"],
    ) -> None:
        """Configure an explicit finite record bound and drop-newest policy.

        Args:
            sink: Application callback invoked only by flush or shutdown.
            capacity: Positive integer record capacity, excluding an in-flight
                flush snapshot of at most the same size. Booleans are rejected.
            drop_policy: The supported policy, drop_newest. Full and shut-down
                exporters discard incoming records and increment dropped.

        Raises:
            ValueError: If capacity or drop_policy is invalid.
        """
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if drop_policy != "drop_newest":
            raise ValueError("drop_policy must be drop_newest")
        self._sink = sink
        self._capacity = capacity
        self._queue: deque[TelemetryRecord] = deque()
        self._lock = Lock()
        self._flush_lock = Lock()
        self._closed = False
        self._dropped = 0
        self._export_errors = 0
        self._stop_reason_counts = dict.fromkeys(STOP_REASONS, 0)

    def __call__(self, record: TelemetryRecord) -> None:
        """Enqueue or drop immediately without invoking the sink."""
        with self._lock:
            match record:
                case CompletionRecord() | RefusalRecord():
                    if record.stop_reason is not None:
                        self._stop_reason_counts[record.stop_reason] += 1
                case LaunchRecord() | AdmissionDecisionRecord():
                    pass
                case unreachable:
                    assert_never(unreachable)
            if self._closed or len(self._queue) >= self._capacity:
                self._dropped += 1
            else:
                self._queue.append(record)

    @property
    def dropped(self) -> int:
        """Return overflow, post-shutdown, and explicit shutdown-discard counts."""
        with self._lock:
            return self._dropped

    @property
    def export_errors(self) -> int:
        """Return discarded records whose sink raised an ordinary exception."""
        with self._lock:
            return self._export_errors

    @property
    def stop_reason_counts(self) -> Mapping[StopReason, int]:
        """Return detached immutable totals for all reasons, including zeros.

        Each completion or refusal offer counts before overflow or shutdown
        drops. Repeated offers count again; exporting never recounts.
        """
        with self._lock:
            return MappingProxyType(self._stop_reason_counts.copy())

    @property
    def queued(self) -> int:
        """Return currently buffered records, excluding an in-flight snapshot."""
        with self._lock:
            return len(self._queue)

    def _export(self, records: tuple[TelemetryRecord, ...]) -> int:
        exported = 0
        for record in records:
            try:
                self._sink(record)
            except Exception:
                with self._lock:
                    self._export_errors += 1
            else:
                exported += 1
        return exported

    def flush(self) -> int:
        """Export one finite snapshot, returning the successful record count.

        This blocks on the application sink, not on new arrivals. The application
        must bound sink latency and must not call flush/shutdown from its sink.
        """
        with self._flush_lock:
            with self._lock:
                records = tuple(self._queue)
                self._queue.clear()
            return self._export(records)

    def shutdown(self, *, flush: bool = True) -> int:
        """Stop admission and flush or count/discard the final snapshot once.

        Args:
            flush: Export pending records when true; otherwise add them to
                dropped. New records are dropped after either choice.

        Returns:
            Successful exports from this shutdown, or zero on repeated calls.
        """
        with self._flush_lock:
            with self._lock:
                if self._closed:
                    return 0
                self._closed = True
                records = tuple(self._queue)
                self._queue.clear()
                if not flush:
                    self._dropped += len(records)
                    return 0
            return self._export(records)
