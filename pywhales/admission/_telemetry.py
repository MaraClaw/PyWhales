"""Owner-locked decision commitment with caller-thread, unlocked delivery."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from time import monotonic
from typing import TYPE_CHECKING, assert_never
from uuid import uuid4

from pywhales.telemetry import AdmissionDecisionRecord, AdmissionId, AdmissionOutcome
from pywhales.telemetry._lifecycle import deliver

from ._errors import (
    AdmissionCancelled,
    AdmissionQueueTimeout,
    AdmissionRejected,
    RejectionReason,
)
from ._state import Outcome

if TYPE_CHECKING:
    from ._owner import AdmissionOwner
    from ._state import Ticket


class Decision:
    """Mutable owner-locked commitment state retaining immutable correlation."""

    def __init__(self, started: float) -> None:
        self.admission_id = AdmissionId(uuid4().hex)
        self._started = started
        self._committed = False

    def commit(self, outcome: AdmissionOutcome) -> AdmissionDecisionRecord | None:
        """Commit once under the owner lock; only the winner owns delivery."""
        if self._committed:
            return None
        self._committed = True
        return AdmissionDecisionRecord(
            self.admission_id, outcome, max(0.0, monotonic() - self._started)
        )


def commit(ticket: Ticket, outcome: AdmissionOutcome) -> AdmissionDecisionRecord | None:
    """Commit an observed activation, leaving dormant/unobserved tickets silent."""
    return None if ticket.decision is None else ticket.decision.commit(outcome)


def close_pending(owner: AdmissionOwner) -> None:
    """Close and commit pending decisions under lock, then deliver the batch."""
    records: list[AdmissionDecisionRecord] = []
    with owner._condition:
        if owner._closed:
            return
        owner._closed = True
        for bucket in owner._buckets.values():
            for ticket in bucket.pending:
                ticket.done = True
                bucket.record(Outcome.REJECTED_CLOSED)
                record = commit(ticket, "closed")
                if record is not None:
                    records.append(record)
            bucket.pending.clear()
            bucket.pending_demand = 0
        owner._condition.notify_all()
    if owner._telemetry_observer is not None:
        for record in records:
            deliver(owner._telemetry_observer, record)


@contextmanager
def acquisition(owner: AdmissionOwner, ticket: Ticket) -> Generator[None]:
    """Serialize activation and decisions, then deliver after releasing the lock.

    Close and cancel may commit while this acquisition waits; their winning
    transition owns delivery, so unwinding the waiter cannot publish twice.
    No stream lock is held by acquisition callers. Resize, release and
    begin-close never publish decisions.
    """
    observer = owner._telemetry_observer
    started = None if observer is None else monotonic()
    record: AdmissionDecisionRecord | None = None
    try:
        with owner._condition:
            if ticket.started:
                raise RuntimeError("admission request is single-use")
            ticket.started = True
            if ticket.cancelled:
                raise AdmissionCancelled()
            if started is not None:
                ticket.decision = Decision(started)
            outcome: AdmissionOutcome = "failed"
            try:
                yield
                outcome = "granted"
            except AdmissionRejected as error:
                match error.reason:
                    case RejectionReason.BUSY:
                        outcome = "busy"
                    case RejectionReason.QUEUE_FULL:
                        outcome = "queue_full"
                    case RejectionReason.PENDING_DEMAND_FULL:
                        outcome = "pending_demand_full"
                    case RejectionReason.OWNER_PENDING_DEMAND_FULL:
                        outcome = "owner_pending_demand_full"
                    case RejectionReason.CLOSED:
                        outcome = "closed"
                    case unreachable:
                        assert_never(unreachable)
                raise
            except AdmissionQueueTimeout:
                outcome = "queue_timeout"
                raise
            except AdmissionCancelled:
                outcome = "cancelled"
                raise
            finally:
                record = commit(ticket, outcome)
    finally:
        if observer is not None and record is not None:
            deliver(observer, record)
