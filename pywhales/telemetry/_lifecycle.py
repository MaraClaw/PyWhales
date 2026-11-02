"""Per-launch metadata state with isolated callback delivery.

Only opaque attempt identifiers, numeric measurements, and closed vocabularies
cross this boundary. Observer exceptions are contained without rendering details.
"""

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock
from time import monotonic
from types import TracebackType
from typing import Self
from uuid import uuid4

from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.telemetry import (
    CompletionRecord,
    LaunchRecord,
    OperationFamily,
    Outcome,
    RefusalOutcome,
    RefusalRecord,
    TelemetryObserver,
    TelemetryRecord,
)

from ._context import current_admission_id


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
    ``ObservationLimitError`` is ``limit``; deadlines stay ``timeout`` and every
    other exception, ``KeyboardInterrupt`` included, is ``failure``.
    """
    if issubclass(error, (DockerTimeout, ObservationTimeout)):
        return "timeout"
    if issubclass(error, ObservationCancelled):
        return "cancelled"
    if issubclass(error, ObservationLimitError):
        return "limit"
    if issubclass(error, GeneratorExit):
        return "closed"
    return "failure"


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
                    ),
                )
            raise

    def captured(self, stdout: bytes | None, stderr: bytes | None) -> None:
        """Measure raw captured bytes before decoding or newline removal."""
        self.stdout_bytes = None if stdout is None else len(stdout)
        self.stderr_bytes = None if stderr is None else len(stderr)

    def finish(self, outcome: Outcome) -> None:
        """Publish the first completion outside the state lock."""
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
        self.finish("success" if exc_type is None else outcome_of(exc_type))
