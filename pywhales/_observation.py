"""Context-local observation limits, deadlines, and cancellation checkpoints."""

import math
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event, Lock
from types import TracebackType
from typing import Self

from pywhales.exceptions import (
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy


@dataclass(slots=True)
class _ResponseAccount:
    """Track actual bytes consumed during one observation scope.

    Workers running in copies of the scope's context share one account, so every
    charge updates ``consumed`` and judges the limit under ``lock``.
    """

    limit: int
    consumed: int = 0
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)


@dataclass(slots=True)
class _StreamAccount(_ResponseAccount):
    """Track the stream and archive bytes read during one observation scope.

    Kept apart from the scope's response account: captured output never counts
    here, and stream bytes never count against ``max_total_response_bytes``.
    """


@dataclass(slots=True)
class _StdinAccount(_ResponseAccount):
    """Track the child stdin bytes a local pipe accepted during one scope.

    Kept apart from the response and stream accounts. ``write_stdin`` holds the
    lock across each write, so ``consumed`` never passes ``limit``.
    """


@dataclass(slots=True)
class _IoAccount(_ResponseAccount):
    """Track every managed byte read from or written to a child during one scope.

    Shared by captured output, stream and archive caller reads, and managed stdin
    writes, on top of their own accounts. ``read_io`` and ``write_stdin`` judge
    and charge it under ``lock`` in one step with the pipe operation, so only an
    output read can leave ``consumed`` one byte past ``limit``; after that
    nothing spends it.
    """


@dataclass(slots=True)
class _LaunchAccount:
    """Accumulate accepted launch reservations shared by copied contexts."""

    limit: int
    consumed: int = 0
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)


@dataclass(slots=True)
class _ChildReservation:
    """Return shared child capacity once, only after reap or failed spawn."""

    accounts: tuple[_LaunchAccount, ...]
    _released: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def release(self) -> None:
        """Release all reserved child slots once without refunding attempts."""
        with self._lock, ExitStack() as locked:
            if self._released:
                return
            for account in self.accounts:
                locked.enter_context(account.lock)
            for account in self.accounts:
                account.consumed -= 1
            self._released = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _charge(accounts: tuple[_ResponseAccount, ...], size: int, reason: str) -> None:
    """Charge every account under its own lock before reporting any overflow."""
    exceeded: tuple[int, int] | None = None
    for account in accounts:
        with account.lock:
            account.consumed += size
            consumed = account.consumed
        if consumed > account.limit and exceeded is None:
            exceeded = (account.limit, consumed)
    if exceeded is not None:
        raise ObservationLimitError(reason, *exceeded)


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationState:
    """Effective nested limits and all active cancellation tokens."""

    policy: InspectionPolicy
    deadline: float | None
    cancel_tokens: tuple[Event, ...]
    response_accounts: tuple[_ResponseAccount, ...]
    stream_accounts: tuple[_StreamAccount, ...] = ()
    stdin_accounts: tuple[_StdinAccount, ...] = ()
    io_accounts: tuple[_IoAccount, ...] = ()
    launch_accounts: tuple[_LaunchAccount, ...] = ()
    child_accounts: tuple[_LaunchAccount, ...] = ()

    def reserve_child(self) -> _ChildReservation:
        """Atomically debit attempts and child slots after ordinary preflight.

        Lock each family outermost first, attempts before child capacity. Every
        allowance is checked before any debit. Only child slots are refundable;
        their token belongs to the reaper, with setup cleanup as a fallback.
        """
        reservation = _ChildReservation(self.child_accounts)
        with ExitStack() as locked:
            for account in (*self.launch_accounts, *self.child_accounts):
                locked.enter_context(account.lock)
            for accounts, reason in (
                (self.launch_accounts, "total_launch_attempts"),
                (self.child_accounts, "concurrent_children"),
            ):
                for account in accounts:
                    measured = account.consumed + 1
                    if measured > account.limit:
                        raise ObservationLimitError(reason, account.limit, measured)
            for account in (*self.launch_accounts, *self.child_accounts):
                account.consumed += 1
        return reservation

    def check_launch_attempts(self, count: int, *, reserve: bool = False) -> None:
        """Check every remaining launch allowance, optionally debiting all of them.

        Lock enclosing accounts outermost first, then check all before updating
        any. Copied contexts share those accounts. A successful reservation is
        never refunded, even if interruption or spawn failure follows.

        Args:
            count: Nonnegative number of planned launches; one for a reservation.
            reserve: Debit the checked count when true. The default only checks
                a complete inventory plan and holds no capacity for it afterward.

        Raises:
            ObservationLimitError: With reason ``total_launch_attempts`` when
                any account cannot fit the count. No account is changed.
        """
        with ExitStack() as locked:
            for account in self.launch_accounts:
                locked.enter_context(account.lock)
            for account in self.launch_accounts:
                measured = account.consumed + count
                if measured > account.limit:
                    raise ObservationLimitError(
                        "total_launch_attempts", account.limit, measured
                    )
            if reserve:
                for account in self.launch_accounts:
                    account.consumed += count

    @property
    def binds_launches(self) -> bool:
        """Whether a stream or archive launched here keeps enforcing this state.

        A launch retains the snapshot only when it has something to enforce after
        the scope's ``with`` block exits: a deadline, a cancel token, or a stream,
        stdin or aggregate I/O budget, or owned-child capacity. Only a deadline or
        a token ever needs a timed wait.
        """
        return (
            self.deadline is not None
            or bool(self.cancel_tokens)
            or bool(self.stream_accounts)
            or bool(self.stdin_accounts)
            or bool(self.io_accounts)
            or bool(self.child_accounts)
        )

    def response_allowance(self, command_remaining: int) -> int:
        """Return the tightest remaining command or scope allowance."""
        return max(
            0,
            min(
                (
                    command_remaining,
                    *(
                        account.limit - account.consumed
                        for account in self.response_accounts
                    ),
                )
            ),
        )

    def charge_response(self, size: int) -> None:
        """Charge all enclosing scopes before reporting any overflow.

        Safe for concurrent charges from threads sharing these accounts: each
        account is updated and compared with its limit under its own lock, so no
        charge is lost and the first account to overflow reports the total it
        reached at that charge.
        """
        _charge(self.response_accounts, size, "total_response")

    def stream_allowance(self, request: int) -> int:
        """Return the largest read that still flags overflow at its first excess byte.

        Args:
            request: Bytes the caller would otherwise read.

        Returns:
            ``request``, or fewer bytes so that no stream account passes its limit
            by more than one byte, and at least one.
        """
        return max(
            1,
            min(
                (
                    request,
                    *(
                        account.limit - account.consumed + 1
                        for account in self.stream_accounts
                    ),
                )
            ),
        )

    def charge_stream(self, size: int) -> None:
        """Charge stream or archive bytes to every enclosing stream account.

        Shares ``charge_response``'s locking and first-overflow reporting, but
        raises reason ``total_stream`` and never touches a response account.
        """
        _charge(self.stream_accounts, size, "total_stream")

    def read_io(
        self, request: int, read: Callable[[int], bytes]
    ) -> tuple[bytes, ObservationLimitError | None]:
        """Run one output read allowed by, and charged to, every aggregate account.

        Every enclosing aggregate account stays locked, outermost first, while
        ``read`` runs, so readers and writers sharing an account never spend it
        between its allowance and the charge. ``read`` must not block: pass a
        nonblocking pipe read and wait for readiness outside this call. The read
        is cut so that an account passes its limit by one detection byte at most.

        Args:
            request: Positive bytes the caller would otherwise read.
            read: Perform one read of at most the given bytes and return them;
                empty at end of file. An exception it raises charges nothing.

        Returns:
            The bytes read, charged to every enclosing account, and the
            ``total_io`` error for the first account the read pushed past its
            limit, else ``None``. The caller applies its own source limits first,
            so one of those wins a same-read tie.

        Raises:
            ObservationLimitError: With reason ``total_io`` when an enclosing
                account was already overflowed. Nothing is read or charged.
        """
        accounts = self.io_accounts
        if not accounts:
            return read(request), None
        with ExitStack() as locked:
            for account in accounts:
                locked.enter_context(account.lock)
            for account in accounts:
                if account.consumed > account.limit:
                    raise ObservationLimitError(
                        "total_io", account.limit, account.consumed
                    )
            chunk = read(
                min(
                    (
                        request,
                        *(account.limit - account.consumed + 1 for account in accounts),
                    )
                )
            )
            overflow: ObservationLimitError | None = None
            for account in accounts:
                account.consumed += len(chunk)
                if overflow is None and account.consumed > account.limit:
                    overflow = ObservationLimitError(
                        "total_io", account.limit, account.consumed
                    )
        return chunk, overflow

    def write_stdin(self, size: int, write: Callable[[int], int]) -> int:
        """Run one pipe write bounded by, and charged to, every stdin and I/O account.

        Every enclosing account stays locked, outermost first, stdin before
        aggregate, while ``write`` runs, so writers sharing an account never pass
        its limit and none holds back capacity another could still use. ``write``
        must not block: pass a nonblocking pipe write and wait for readiness
        outside this call.

        Args:
            size: Positive bytes the caller still wants to send.
            write: Perform one write of at most the given bytes and return how
                many the pipe accepted. An exception it raises charges nothing.

        Returns:
            Bytes the pipe accepted, charged to every enclosing account; fewer
            than ``size`` when cut to the tightest remaining allowance.

        Raises:
            ObservationLimitError: With reason ``total_stdin``, or ``total_io``
                when only an aggregate account is spent, when an enclosing account
                has no allowance left. Nothing is written or charged.
        """
        accounts = (*self.stdin_accounts, *self.io_accounts)
        with ExitStack() as locked:
            for account in accounts:
                locked.enter_context(account.lock)
            for account in accounts:
                if account.consumed >= account.limit:
                    raise ObservationLimitError(
                        "total_io"
                        if isinstance(account, _IoAccount)
                        else "total_stdin",
                        account.limit,
                        max(account.limit + 1, account.consumed),
                    )
            written = write(
                min((size, *(account.limit - account.consumed for account in accounts)))
            )
            for account in accounts:
                account.consumed += written
        return written


_current_observation: ContextVar[ObservationState | None] = ContextVar(
    "pywhales_observation", default=None
)


def get_observation() -> ObservationState | None:
    """Return the effective state for the current context, if any."""
    return _current_observation.get()


def _minimum_optional(first: int | None, second: int | None) -> int | None:
    limits = [value for value in (first, second) if value is not None]
    return min(limits) if limits else None


def check_observation(state: ObservationState | None = None) -> None:
    """Raise safely if the supplied or current observation is cancelled or expired."""
    current = state if state is not None else get_observation()
    if current is None:
        return
    if any(token.is_set() for token in current.cancel_tokens):
        raise ObservationCancelled()
    if current.deadline is not None and time.monotonic() >= current.deadline:
        raise ObservationTimeout()


@contextmanager
def observation(
    policy: InspectionPolicy,
    *,
    deadline: float | None = None,
    cancel: Event | None = None,
) -> Generator[ObservationState]:
    """Apply nested bounds until exit, checking cancellation and deadline at both ends.

    Args:
        policy: Explicit bounds, intersected with any enclosing scope.
        deadline: Finite absolute monotonic timestamp in seconds, or no deadline.
        cancel: Optional token; every enclosing token remains effective.

    Yields:
        Effective state suitable for explicit checkpoints in other contexts.

    Raises:
        ValueError: If the deadline is not finite.
        ObservationCancelled: If any active cancellation token is set.
        ObservationTimeout: If the effective deadline has expired.
    """
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("Observation deadline must be finite")
    parent = get_observation()
    response_limit = policy.max_total_response_bytes
    stream_limit = policy.max_total_stream_bytes
    stdin_limit = policy.max_total_stdin_bytes
    io_limit = policy.max_total_io_bytes
    launch_limit = policy.max_total_launch_attempts
    child_limit = policy.max_concurrent_children
    if parent is not None:
        policy = InspectionPolicy(
            max_references=min(parent.policy.max_references, policy.max_references),
            max_argv_bytes=min(parent.policy.max_argv_bytes, policy.max_argv_bytes),
            max_response_bytes=min(
                parent.policy.max_response_bytes, policy.max_response_bytes
            ),
            max_total_references=_minimum_optional(
                parent.policy.max_total_references, policy.max_total_references
            ),
            max_total_response_bytes=_minimum_optional(
                parent.policy.max_total_response_bytes, policy.max_total_response_bytes
            ),
            max_total_stream_bytes=_minimum_optional(
                parent.policy.max_total_stream_bytes, policy.max_total_stream_bytes
            ),
            max_total_stdin_bytes=_minimum_optional(
                parent.policy.max_total_stdin_bytes, policy.max_total_stdin_bytes
            ),
            max_total_io_bytes=_minimum_optional(
                parent.policy.max_total_io_bytes, policy.max_total_io_bytes
            ),
            max_total_launch_attempts=_minimum_optional(
                parent.policy.max_total_launch_attempts,
                policy.max_total_launch_attempts,
            ),
            max_concurrent_children=_minimum_optional(
                parent.policy.max_concurrent_children, policy.max_concurrent_children
            ),
        )
    deadlines = tuple(
        value
        for value in (parent.deadline if parent else None, deadline)
        if value is not None
    )
    tokens = parent.cancel_tokens if parent is not None else ()
    accounts = parent.response_accounts if parent is not None else ()
    stream_accounts = parent.stream_accounts if parent is not None else ()
    stdin_accounts = parent.stdin_accounts if parent is not None else ()
    io_accounts = parent.io_accounts if parent is not None else ()
    launch_accounts = parent.launch_accounts if parent is not None else ()
    child_accounts = parent.child_accounts if parent is not None else ()
    state = ObservationState(
        policy=policy,
        deadline=min(deadlines) if deadlines else None,
        cancel_tokens=(*tokens, cancel) if cancel is not None else tokens,
        response_accounts=(
            (*accounts, _ResponseAccount(response_limit))
            if response_limit is not None
            else accounts
        ),
        stream_accounts=(
            (*stream_accounts, _StreamAccount(stream_limit))
            if stream_limit is not None
            else stream_accounts
        ),
        stdin_accounts=(
            (*stdin_accounts, _StdinAccount(stdin_limit))
            if stdin_limit is not None
            else stdin_accounts
        ),
        io_accounts=(
            (*io_accounts, _IoAccount(io_limit))
            if io_limit is not None
            else io_accounts
        ),
        launch_accounts=(
            (*launch_accounts, _LaunchAccount(launch_limit))
            if launch_limit is not None
            else launch_accounts
        ),
        child_accounts=(
            (*child_accounts, _LaunchAccount(child_limit))
            if child_limit is not None
            else child_accounts
        ),
    )
    token = _current_observation.set(state)
    try:
        check_observation(state)
        yield state
        check_observation(state)
    finally:
        _current_observation.reset(token)
