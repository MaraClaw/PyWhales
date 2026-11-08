"""Fail-closed rejection types shared by the delivery chain."""

from dataclasses import dataclass
from typing import override


@dataclass(slots=True)
class Rejected(Exception):
    """Fail closed; exception traceback metadata must remain mutable."""

    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(slots=True)
class CurlFailed(Rejected):
    """Curl exited nonzero; the exit code classifies it without exposing stderr."""

    code: int


def require(condition: bool, reason: str) -> None:
    """Reject an unsatisfied trust invariant."""
    if not condition:
        raise Rejected(reason)
