"""Simulation artifacts with explicit collection boundaries and ownership."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Self, assert_never

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from scripts.fleet_contract.common import (
    Amount,
    Contract,
    Count,
    Digest,
    Positive,
    PositiveCount,
    SchemaVersion,
    Token,
)
from scripts.fleet_contract.platform import Platform, Topology
from scripts.fleet_contract.results import Outcome, Outcomes
from scripts.fleet_contract.workload import (
    Action,
    Artifact,
    Boundary,
    Image,
    Workload,
)

from .models import Metric, Step
from .timeline import action_peak_counts, is_ordered, peak_counts, reconcile


class OwnedResource(Contract):
    """Exact acquired identity, scoped to endpoint, namespace and marker."""

    endpoint: Token
    kind: Literal["container", "service", "image", "volume", "network", "cache", "log"]
    identity: str
    slot: Token
    namespace: Token
    marker: Token


class Observation(Contract):
    """One terminal population observation, including failure and unavailable time."""

    submission: Count
    scenario: Token
    boundary: Boundary
    outcome: Outcome
    seconds: Amount | None
    reason: Literal["observed", "unavailable", "operation_failed", "deadline"]
    started_at_seconds: Amount
    ended_at_seconds: Amount


class Distribution(Contract):
    """Nearest-rank summary of all observed outcomes, never successes only."""

    boundary: Boundary
    recorded: Count
    unavailable: Count
    values_seconds: tuple[Amount, ...]
    p50_seconds: Amount | None
    p95_seconds: Amount | None
    maximum_seconds: Amount | None


class Cleanup(Contract):
    """Bounded exact-identity cleanup receipt and unexpected remaining assets."""

    attempted: tuple[OwnedResource, ...]
    removed: tuple[OwnedResource, ...]
    residual: tuple[OwnedResource, ...]
    elapsed_seconds: Amount
    timed_out: bool


class Environment(Contract):
    """Local interpreter observations and explicitly declared simulated target."""

    python: str
    library: str
    docker_cli: None = None
    docker_engine: None = None
    compose: None = None
    buildx: None = None
    kernel: None = None
    host: None = None
    target_metadata: Literal["declared_not_observed"] = "declared_not_observed"
    platform: Platform
    topology: Topology
    images: tuple[Image, ...]

    @field_validator("platform")
    @classmethod
    def withhold_connections(cls, value: Platform) -> Platform:
        """Withhold private references for generated and imported artifacts alike."""
        return value.model_copy(
            update={
                "endpoints": tuple(
                    endpoint.model_copy(update={"connection_ref": "withheld"})
                    for endpoint in value.endpoints
                )
            }
        )


def fingerprint(value: Contract) -> str:
    """Hash normalized JSON with defaults, sorted keys and preserved array order."""
    encoded = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class Binding(Contract):
    """Versioned content fingerprints, not authenticated provenance."""

    version: SchemaVersion
    manifest_digest: Digest
    environment_digest: Digest


class QueueAttempt(Contract):
    """One half-open queue interval for a logical submission."""

    enqueued_seconds: Amount
    ended_seconds: Amount


class QueueBackoff(Contract):
    """One half-open interval outside queue and active capacity after lease expiry."""

    started_seconds: Amount
    ended_seconds: Amount


class Receipt(Contract):
    """Arrival, admission and terminal time of one submission.

    A null admission means the submission never launched: it timed out in the
    queue or was cancelled there by an early stop.
    """

    submission: PositiveCount
    action: Action
    arrival_seconds: Amount
    admitted_seconds: Amount | None
    terminal_seconds: Amount
    outcome: Outcome
    attempts: tuple[QueueAttempt, ...] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    backoffs: tuple[QueueBackoff, ...] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


class Peaks(Contract):
    """Largest settled active and queued submission counts over one run."""

    active: Count
    queued: Count
    per_action: tuple[tuple[Action, Count], ...] = ()


class Schedule(Contract):
    """Frozen overlap receipts in submission order with their observed peaks."""

    version: Literal[1, 2, 3, 4] = Field(default=1, exclude_if=lambda v: v == 1)
    peaks: Peaks
    receipts: tuple[Receipt, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject receipts that break FIFO order or peaks the receipts do not give."""
        if self.version == 1 and any(r.backoffs is not None for r in self.receipts):
            raise PydanticCustomError("schedule_binding", "schedule_binding")
        if not is_ordered(self.receipts):
            raise PydanticCustomError("schedule_order", "schedule_order")
        if (self.peaks.active, self.peaks.queued) != peak_counts(
            self.receipts
        ) or self.peaks.per_action != action_peak_counts(self.receipts):
            raise PydanticCustomError("schedule_peaks", "schedule_peaks")
        return self


class RunReport(Contract):
    """Always simulated and unqualified, irrespective of scripted success."""

    schema_version: Literal[1] = 1
    simulation: Literal[True] = True
    qualification: Literal["unqualified"] = "unqualified"
    real_qualification: Literal["blocked_binding_and_deadline_adapter"] = (
        "blocked_binding_and_deadline_adapter"
    )
    clock: Literal["fake_monotonic_seconds"] = "fake_monotonic_seconds"
    status: Literal["completed", "stopped", "rejected"]
    reasons: tuple[str, ...]
    manifest_revision: Digest
    workload_digest: Digest
    operation_digest: Digest
    workload: Workload
    repetitions: Count
    steps: tuple[Step, ...]
    operation_timeout_seconds: Positive
    environment: Environment
    binding: Binding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    artifacts: tuple[Artifact, ...]
    provider_calls: Count
    connections: Literal[0] = 0
    command_count: Count
    submitted: Count
    outcomes: Outcomes
    samples: tuple[Observation, ...]
    distributions: tuple[Distribution, ...]
    metrics: tuple[Metric, ...]
    ledger: tuple[OwnedResource, ...]
    cleanup: Cleanup
    elapsed_seconds: Amount
    load_elapsed_seconds: Amount
    metrics_boundary: Literal["after_each_submission_simulated"] = (
        "after_each_submission_simulated"
    )
    schedule: Schedule | None = None

    @field_validator("binding", mode="before")
    @classmethod
    def binding_present_is_not_null(cls, value: object) -> object:
        """Allow legacy absence, but reject an explicitly incomplete binding."""
        if value is None:
            raise PydanticCustomError("binding_invalid", "binding_invalid")
        return value

    @model_validator(mode="after")
    def coherent_binding(self) -> Self:
        """Verify the retained, sanitized environment before accepting evidence."""
        if self.binding is not None and self.binding.environment_digest != fingerprint(
            self.environment
        ):
            raise PydanticCustomError("binding_environment", "binding_environment")
        return self

    @model_validator(mode="after")
    def coherent_outcomes(self) -> Self:
        """Reject imported populations that contradict their terminal samples."""
        queued = [sample for sample in self.samples if sample.boundary == "queue_wait"]
        if [sample.submission for sample in queued] != list(
            range(1, self.submitted + 1)
        ) or self.outcomes != terminal_outcomes(self.samples):
            raise PydanticCustomError("outcome_population", "outcome_population")
        submissions = {sample.submission: sample for sample in queued}
        for sample in self.samples:
            queue = submissions.get(sample.submission)
            if queue is None or (sample.scenario, sample.outcome) != (
                queue.scenario,
                queue.outcome,
            ):
                raise PydanticCustomError("outcome_population", "outcome_population")
        if self.command_count != sum(
            sample.boundary == "selected_cli" for sample in self.samples
        ):
            raise PydanticCustomError("command_population", "command_population")
        return self

    @model_validator(mode="after")
    def coherent_execution(self) -> Self:
        """Reject call accounting or status reasons that contradict execution."""
        if self.provider_calls != self.command_count + len(self.cleanup.attempted):
            raise PydanticCustomError(
                "provider_call_accounting", "provider_call_accounting"
            )
        match self.status:
            case "completed":
                if self.reasons:
                    raise PydanticCustomError("status_reasons", "status_reasons")
            case "stopped" | "rejected":
                if not self.reasons:
                    raise PydanticCustomError("status_reasons", "status_reasons")
            case unreachable:
                assert_never(unreachable)
        return self

    @model_validator(mode="after")
    def coherent_schedule(self) -> Self:
        """Treat a report without receipts as sequential; verify scheduled ones."""
        code = reconcile(self)
        if code is not None:
            raise PydanticCustomError(code, code)
        return self


@dataclass(frozen=True, slots=True)
class Load:
    """What a load loop observed: samples, submissions, stop reasons and receipts."""

    samples: tuple[Observation, ...]
    submitted: int
    reasons: tuple[str, ...]
    schedule: Schedule | None = None


def terminal_outcomes(samples: tuple[Observation, ...]) -> Outcomes:
    """Count each submission once, independently of its measurement boundaries."""
    terminal = [sample.outcome for sample in samples if sample.boundary == "queue_wait"]
    return Outcomes(
        succeeded=terminal.count("succeeded"),
        failed=terminal.count("failed"),
        timed_out=terminal.count("timed_out"),
        unknown=terminal.count("unknown"),
        cancelled=terminal.count("cancelled"),
        rejected=terminal.count("rejected"),
    )
