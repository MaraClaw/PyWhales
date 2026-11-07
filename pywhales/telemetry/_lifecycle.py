"""Per-launch metadata state with isolated callback delivery.

Only opaque attempt identifiers, numeric measurements, and closed vocabularies
cross this boundary. Observer exceptions are contained without rendering details.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from threading import Lock
from time import monotonic
from types import MappingProxyType, TracebackType
from typing import Final, Self, assert_never
from uuid import uuid4

from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
    StreamBufferLimitError,
)
from pywhales.telemetry import (
    CompletionRecord,
    LaunchRecord,
    OperationFamily,
    Outcome,
    RefusalOutcome,
    RefusalRecord,
    StopReason,
    TelemetryObserver,
    TelemetryRecord,
)

from ._context import current_admission_id
from ._records import STOP_REASONS

_EXCLUDED_FROM_LIMITS: Final[frozenset[StopReason]] = frozenset(
    {
        "command_timeout",
        "scope_timeout",
        "cancelled",
        "buffer_output_record",
        "buffer_stderr_history",
        "other_limit",
    }
)
_LIMIT_REASONS: Final[Mapping[str, StopReason]] = MappingProxyType(
    {reason: reason for reason in STOP_REASONS if reason not in _EXCLUDED_FROM_LIMITS}
)


def deliver(observer: TelemetryObserver, record: TelemetryRecord) -> None:
    """Invoke application code without exposing or propagating ordinary errors."""
    try:
        observer(record)
    except Exception:
        return


def outcome_of(error: type[BaseException]) -> Outcome:
    """Map an exception class to the one completion outcome that reports it.

    The same rule serves captured runs, archives, stream launches, and the error
    a stream delivers. ``ObservationCancelled`` is ``cancelled`` and
    ``ObservationLimitError`` and ``StreamBufferLimitError`` are ``limit``;
    deadlines stay ``timeout`` and every other exception, ``KeyboardInterrupt``
    and a builtin ``BufferError`` included, is ``failure``.
    """
    if issubclass(error, (DockerTimeout, ObservationTimeout)):
        return "timeout"
    if issubclass(error, ObservationCancelled):
        return "cancelled"
    if issubclass(error, (ObservationLimitError, StreamBufferLimitError)):
        return "limit"
    if issubclass(error, GeneratorExit):
        return "closed"
    return "failure"


def stop_reason_of(error: BaseException | None) -> StopReason | None:
    """Project one exception instance onto the closed stop-reason vocabulary.

    A launch deadline is ``command_timeout``, an observation deadline
    ``scope_timeout`` and a set token ``cancelled``. An observation limit keeps
    its reason only when the vocabulary recognizes it; any other string becomes
    ``other_limit``, so arbitrary text and keys never reach a record or counter.
    A stream buffer overflow maps by its closed category, never by message text.

    Args:
        error: The exception that ended the launch, or None for no exception.

    Returns:
        The matching reason, or None for success, closure and ordinary failures.
    """
    match error:
        case ObservationTimeout():
            return "scope_timeout"
        case DockerTimeout():
            return "command_timeout"
        case ObservationCancelled():
            return "cancelled"
        case ObservationLimitError():
            return _LIMIT_REASONS.get(error.reason, "other_limit")
        case StreamBufferLimitError():
            match error.category:
                case "output_record":
                    return "buffer_output_record"
                case "stderr_history":
                    return "buffer_stderr_history"
                case unreachable:
                    assert_never(unreachable)
        case _:
            return None


class ExecutionTelemetry:
    """Accumulate one launch's measurements and publish completion at most once.

    Start only at the local launch seam after preflight validation. No observer
    means no clock or callback. Applications must keep callbacks nonblocking and
    must not mutate execution state or reenter owned streams from callbacks.
    """

    def __init__(
        self, observer: TelemetryObserver | None, family: OperationFamily
    ) -> None:
        self._observer = observer
        self._family: OperationFamily = family
        self._started: float | None = None
        self._attempt_id: str | None = None
        self._finished = False
        self._lock = Lock()
        self.stdout_bytes: int | None = None
        self.stderr_bytes: int | None = None
        self.stdin_bytes: int | None = None
        self.return_code: int | None = None

    def start(self) -> None:
        """Emit launch before starting the local clock and attempting spawn."""
        if self._observer is not None:
            self._attempt_id = uuid4().hex
            deliver(
                self._observer,
                LaunchRecord(
                    self._family,
                    attempt_id=self._attempt_id,
                    admission_id=current_admission_id.get(),
                ),
            )
            self._started = monotonic()

    @contextmanager
    def preflight(self) -> Generator[None]:
        """Report an observation refusal without changing its exception."""
        try:
            yield
        except (
            ObservationCancelled,
            ObservationTimeout,
            ObservationLimitError,
        ) as error:
            if self._observer is not None:
                match error:
                    case ObservationCancelled():
                        outcome: RefusalOutcome = "cancelled"
                    case ObservationTimeout():
                        outcome = "timeout"
                    case ObservationLimitError():
                        outcome = "limit"
                deliver(
                    self._observer,
                    RefusalRecord(
                        self._family,
                        outcome,
                        attempt_id=uuid4().hex,
                        admission_id=current_admission_id.get(),
                        stop_reason=stop_reason_of(error),
                    ),
                )
            raise

    def captured(self, stdout: bytes | None, stderr: bytes | None) -> None:
        """Measure raw captured bytes before decoding or newline removal."""
        self.stdout_bytes = None if stdout is None else len(stdout)
        self.stderr_bytes = None if stderr is None else len(stderr)

    def finish(
        self, outcome: Outcome, *, stop_reason: StopReason | None = None
    ) -> None:
        """Publish the first completion outside the state lock.

        Args:
            outcome: Closed result of the launch.
            stop_reason: Boundary that ended a timeout, cancelled or limit
                outcome, taken from the same exception as ``outcome``.
        """
        with self._lock:
            if (
                self._observer is None
                or self._started is None
                or self._attempt_id is None
                or self._finished
            ):
                return
            self._finished = True
            record = CompletionRecord(
                self._family,
                outcome,
                max(0, monotonic() - self._started),
                self.stdout_bytes,
                self.stderr_bytes,
                attempt_id=self._attempt_id,
                return_code=self.return_code,
                stdin_bytes=self.stdin_bytes,
                stop_reason=stop_reason,
            )
        deliver(self._observer, record)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Report captured completion without changing an active exception."""
        self.finish(
            "success" if exc_type is None else outcome_of(exc_type),
            stop_reason=stop_reason_of(exc_value),
        )
