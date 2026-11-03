"""Admission failures containing no operation or connection payloads."""

from enum import Enum


class AdmissionError(Exception):
    """Base class for admission failures, distinct from operation failures."""


class RejectionReason(Enum):
    """Bounded reasons for rejecting admission before invoking work."""

    BUSY = "busy"
    QUEUE_FULL = "queue_full"
    CLOSED = "closed"
    PENDING_DEMAND_FULL = "pending_demand_full"
    OWNER_PENDING_DEMAND_FULL = "owner_pending_demand_full"


class AdmissionRejected(AdmissionError):
    """Admission rejected because capacity is exhausted or the owner is closed."""

    def __init__(self, reason: RejectionReason) -> None:
        """Retain only the bounded rejection reason."""
        self.reason = reason
        super().__init__(f"admission rejected: {reason.value}")


class AdmissionCancelled(AdmissionError):
    """Request cancelled before admission won the race."""

    def __init__(self) -> None:
        super().__init__("admission cancelled")


class AdmissionQueueTimeout(AdmissionError):
    """Queue deadline expired before active admission."""

    def __init__(self) -> None:
        super().__init__("admission queue timed out")


class AdmissionReentryError(AdmissionError):
    """Same-context nesting through the same owner is unsupported."""

    def __init__(self) -> None:
        super().__init__("same-owner admission reentry is not supported")
