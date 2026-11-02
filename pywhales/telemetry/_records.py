"""Optional, payload-free local execution metadata and application-owned export.

Records never contain argv, endpoints, environment, output, or exception text,
even when raw diagnostics are enabled. No monitoring dependency is required.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, NewType

type OperationFamily = Literal["captured", "stream", "archive_load", "archive_save"]
type Outcome = Literal["success", "failure", "timeout", "closed", "cancelled", "limit"]
type RefusalOutcome = Literal["cancelled", "timeout", "limit"]
AdmissionId = NewType("AdmissionId", str)
type AdmissionOutcome = Literal[
    "granted", "busy", "queue_full", "closed", "queue_timeout", "cancelled", "failed"
]


@dataclass(frozen=True, slots=True)
class AdmissionDecisionRecord:
    """Payload-free decision for one activated admission request.

    Attributes:
        admission_id: Opaque identifier shared with admitted launch records.
        outcome: Closed admission result, independent of subsequent work success.
        duration_seconds: Nonnegative monotonic seconds from activation to decision.
        schema_version: Record schema version, currently 6.
        phase: Fixed admission decision discriminator.
    """

    admission_id: AdmissionId
    outcome: AdmissionOutcome
    duration_seconds: float
    schema_version: Literal[6] = field(default=6, init=False)
    phase: Literal["admission"] = field(default="admission", init=False)


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """Versioned metadata emitted immediately before a local launch attempt.

    Attributes:
        family: Fixed execution family, never inferred from command arguments.
        attempt_id: Opaque identifier shared with the completion record.
        admission_id: Innermost admitted request, or None outside observed admission.
        schema_version: Record schema version, currently 6.
        phase: Fixed launch discriminator. Measurements are not yet available.
    """

    family: OperationFamily
    attempt_id: str = field(kw_only=True)
    admission_id: AdmissionId | None = field(default=None, kw_only=True)
    schema_version: Literal[6] = field(default=6, init=False)
    phase: Literal["launch"] = field(default="launch", init=False)


@dataclass(frozen=True, slots=True)
class CompletionRecord:
    """Consumer-visible local completion, not remote workload status.

    Attributes:
        family: Fixed execution family matching the launch record.
        outcome: Success, failure, timeout, explicit consumer closure, an
            observation cancellation, or an observation limit rejection.
        duration_seconds: Monotonic seconds from launch attempt to termination
            or stream closure/exhaustion, including paused stream consumption.
        stdout_bytes: Bytes actually captured/read, or None when unavailable.
        stderr_bytes: Bytes actually captured/read, or None when unavailable.
        stdin_bytes: Bytes accepted by successful local pipe writes, including
            partial progress before failure, or None when unobserved. This does
            not measure bytes processed by the child or selected Docker daemon.
        attempt_id: Opaque identifier shared with the launch record.
        return_code: Reaped local process status, or None if unavailable.
        schema_version: Record schema version, currently 6.
        phase: Fixed completion discriminator.
    """

    family: OperationFamily
    outcome: Outcome
    duration_seconds: float
    stdout_bytes: int | None = None
    stderr_bytes: int | None = None
    attempt_id: str = field(kw_only=True)
    return_code: int | None = field(default=None, kw_only=True)
    stdin_bytes: int | None = field(default=None, kw_only=True)
    schema_version: Literal[6] = field(default=6, init=False)
    phase: Literal["completion"] = field(default="completion", init=False)


@dataclass(frozen=True, slots=True)
class RefusalRecord:
    """Payload-free observation refusal before a local launch.

    Attributes:
        family: Fixed execution family.
        outcome: Cancellation, observation deadline, or observation limit.
        attempt_id: Opaque identifier for this refused attempt.
        admission_id: Innermost observed admission, or None.
        schema_version: Record schema version, currently 6.
        phase: Fixed refusal discriminator.
    """

    family: OperationFamily
    outcome: RefusalOutcome
    attempt_id: str = field(kw_only=True)
    admission_id: AdmissionId | None = field(default=None, kw_only=True)
    schema_version: Literal[6] = field(default=6, init=False)
    phase: Literal["refusal"] = field(default="refusal", init=False)


type TelemetryRecord = (
    LaunchRecord | CompletionRecord | RefusalRecord | AdmissionDecisionRecord
)
type TelemetryObserver = Callable[[TelemetryRecord], None]
