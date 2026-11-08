"""Context-local monotonic deadline shared by transport retries and polling."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from .failures import require

_expiry: ContextVar[float | None] = ContextVar("delivery_expiry", default=None)


@contextmanager
def command_deadline(seconds: float) -> Generator[None]:
    """Bound the enclosed delivery work to `seconds` of monotonic time.

    The deadline is read implicitly by `budget` and `afford`, so
    transports created without arguments still honor it. Scopes do not nest.

    Args:
        seconds: Time allowed from entry until the deadline passes.
    """
    token = _expiry.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _expiry.reset(token)


def remaining() -> float | None:
    """Return seconds left before the deadline, or None outside a deadline scope."""
    expiry = _expiry.get()
    return None if expiry is None else expiry - time.monotonic()


def budget(cap: float) -> float:
    """Return `cap` shortened to the command deadline; reject once it has passed."""
    left = remaining()
    require(left is None or left > 0, "delivery deadline exceeded")
    return cap if left is None else min(cap, left)


def afford(seconds: float) -> None:
    """Reject a wait that would not finish before the command deadline."""
    left = remaining()
    require(left is None or seconds < left, "delivery deadline exceeded")
