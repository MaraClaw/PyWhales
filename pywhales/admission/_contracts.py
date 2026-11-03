"""Immutable admission configuration and bounded reporting values."""

import math
import re
from dataclasses import dataclass
from enum import Enum


def _validate_token(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None
    ):
        raise ValueError("identity must be a safe ASCII token of 1 to 64 characters")


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    """Safe reporting alias, not a connection string or a global owner key."""

    alias: str

    def __post_init__(self) -> None:
        _validate_token(self.alias)


@dataclass(frozen=True, slots=True)
class PressureClass:
    """Safe application-defined admission class name."""

    name: str

    def __post_init__(self) -> None:
        _validate_token(self.name)


class SaturationPolicy(Enum):
    """Behavior when a class has no immediately available capacity."""

    WAIT = "wait"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ClassLimit:
    """Request-count limits with optional active and queued demand capacities.

    None leaves the corresponding demand total unbounded. pending_demand_capacity
    accepts zero and applies only when queueing, never to an immediate grant.
    Reservations account only for participating work, not daemon memory or CPU.
    """

    active: int
    pending: int
    policy: SaturationPolicy = SaturationPolicy.WAIT
    demand_capacity: int | None = None
    pending_demand_capacity: int | None = None

    def __post_init__(self) -> None:
        if type(self.active) is not int or self.active <= 0:
            raise ValueError("active must be a positive integer")
        if type(self.pending) is not int or self.pending < 0:
            raise ValueError("pending must be a nonnegative integer")
        if not isinstance(self.policy, SaturationPolicy):
            raise ValueError("policy must be a SaturationPolicy")
        if self.demand_capacity is not None:
            validate_demand(self.demand_capacity)
        if self.pending_demand_capacity is not None and (
            type(self.pending_demand_capacity) is not int
            or self.pending_demand_capacity < 0
        ):
            raise ValueError("pending_demand_capacity must be a nonnegative integer")


def validate_demand(demand: int, capacity: int | None = None) -> None:
    """Reject noninteger, boolean, nonpositive, or over-capacity demand."""
    if type(demand) is not int or demand <= 0:
        raise ValueError("demand must be a positive integer")
    if capacity is not None and demand > capacity:
        raise ValueError("demand exceeds the class demand capacity")


def validate_queue_timeout(timeout: float | None) -> None:
    """Reject nonpositive, boolean, or nonfinite queue timeouts."""
    if timeout is not None and (
        type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("queue_timeout must be positive finite seconds or None")


def validate_drain_timeout(timeout: float) -> None:
    """Reject nonpositive, boolean, or nonfinite drain timeouts."""
    if (
        type(timeout) not in (int, float)
        or timeout <= 0
        or (type(timeout) is float and not math.isfinite(timeout))
    ):
        raise ValueError("timeout must be positive finite seconds")


@dataclass(frozen=True, slots=True)
class OutcomeCounts:
    """Fixed uint64-saturating outcome counts; saturated marks a reached ceiling."""

    completed: int
    failed: int
    stream_closed: int
    rejected_busy: int
    rejected_full: int
    cancelled: int
    queue_timed_out: int
    rejected_closed: int
    saturated: bool
    rejected_pending_demand_full: int = 0
    rejected_owner_pending_demand_full: int = 0


@dataclass(frozen=True, slots=True)
class DemandSnapshot:
    """Exact reserved active demand, queued demand, and independent high waters."""

    active: int
    pending: int
    active_high_water: int
    pending_high_water: int


@dataclass(frozen=True, slots=True)
class ClassSnapshot:
    """Exact request counts and separate demand at a snapshot instant."""

    pressure_class: PressureClass
    limit: ClassLimit
    active: int
    pending: int
    active_high_water: int
    pending_high_water: int
    outcomes: OutcomeCounts
    demand: DemandSnapshot = DemandSnapshot(0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    """Safe endpoint alias, terminal closure state, and bounded class snapshots."""

    endpoint: EndpointIdentity
    classes: tuple[ClassSnapshot, ...]
    closed: bool
