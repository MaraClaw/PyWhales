"""Mutable lock-owned admission tickets, occupancy, and fixed counters."""

from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Final

from pywhales.telemetry import AdmissionId

from ._contracts import (
    ClassLimit,
    ClassSnapshot,
    DemandSnapshot,
    OutcomeCounts,
    PressureClass,
)

if TYPE_CHECKING:
    from ._telemetry import Decision

UINT64_MAX: Final = (1 << 64) - 1


class Outcome(IntEnum):
    """Fixed indexes into bounded per-class counters."""

    COMPLETED = 0
    FAILED = 1
    STREAM_CLOSED = 2
    REJECTED_BUSY = 3
    REJECTED_FULL = 4
    CANCELLED = 5
    QUEUE_TIMED_OUT = 6
    REJECTED_CLOSED = 7
    REJECTED_PENDING_DEMAND_FULL = 8
    REJECTED_OWNER_PENDING_DEMAND_FULL = 9


@dataclass(eq=False, slots=True)
class Ticket:
    """Mutable request transitions protected by its owner's condition lock."""

    demand: int = 1
    started: bool = False
    cancelled: bool = False
    active: bool = False
    done: bool = False
    closing: bool = False
    decision: Decision | None = None

    @property
    def admission_id(self) -> AdmissionId | None:
        """Return correlation retained after the admission decision commits."""
        return None if self.decision is None else self.decision.admission_id


class Bucket:
    """Lock-owned mutable occupancy and counters for one configured class."""

    def __init__(self, limit: ClassLimit) -> None:
        self.limit = limit
        self.active = 0
        self.pending: deque[Ticket] = deque()
        self.active_high_water = 0
        self.pending_high_water = 0
        self.active_demand = 0
        self.pending_demand = 0
        self.active_demand_high_water = 0
        self.pending_demand_high_water = 0
        self.counts = [0] * len(Outcome)
        self.saturated = False

    def fits(self, demand: int) -> bool:
        """Check demand capacity; the caller owns the admission lock."""
        capacity = self.limit.demand_capacity
        return capacity is None or self.active_demand + demand <= capacity

    def enqueue(self, ticket: Ticket) -> None:
        """Account for one pending ticket under the admission lock."""
        self.pending.append(ticket)
        self.pending_demand += ticket.demand
        self.pending_high_water = max(self.pending_high_water, len(self.pending))
        self.pending_demand_high_water = max(
            self.pending_demand_high_water, self.pending_demand
        )

    def dequeue(self, ticket: Ticket) -> None:
        """Remove a still-pending ticket exactly once under the admission lock."""
        if ticket in self.pending:
            self.pending.remove(ticket)
            self.pending_demand -= ticket.demand

    def reserve(self, ticket: Ticket) -> None:
        """Account for one active grant under the admission lock."""
        self.resize(ticket, ticket.demand)
        ticket.active = True
        self.active += 1
        self.active_high_water = max(self.active_high_water, self.active)

    def resize(self, ticket: Ticket, demand: int) -> None:
        """Publish active demand under the admission lock after policy checks."""
        self.active_demand += demand - (ticket.demand if ticket.active else 0)
        ticket.demand = demand
        self.active_demand_high_water = max(
            self.active_demand_high_water, self.active_demand
        )

    def record(self, outcome: Outcome) -> None:
        self.counts[outcome] = min(UINT64_MAX, self.counts[outcome] + 1)
        self.saturated |= self.counts[outcome] == UINT64_MAX

    def snapshot(self, pressure_class: PressureClass) -> ClassSnapshot:
        return ClassSnapshot(
            pressure_class,
            self.limit,
            self.active,
            len(self.pending),
            self.active_high_water,
            self.pending_high_water,
            OutcomeCounts(
                self.counts[Outcome.COMPLETED],
                self.counts[Outcome.FAILED],
                self.counts[Outcome.STREAM_CLOSED],
                self.counts[Outcome.REJECTED_BUSY],
                self.counts[Outcome.REJECTED_FULL],
                self.counts[Outcome.CANCELLED],
                self.counts[Outcome.QUEUE_TIMED_OUT],
                self.counts[Outcome.REJECTED_CLOSED],
                self.saturated,
                self.counts[Outcome.REJECTED_PENDING_DEMAND_FULL],
                self.counts[Outcome.REJECTED_OWNER_PENDING_DEMAND_FULL],
            ),
            DemandSnapshot(
                self.active_demand,
                self.pending_demand,
                self.active_demand_high_water,
                self.pending_demand_high_water,
            ),
        )
