"""Offline observations preserving every outcome and unavailable measurement."""

from typing import Literal, Self, assert_never

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .common import (
    Amount,
    Contract,
    Count,
    Digest,
    Nonempty,
    PositiveCount,
    SchemaVersion,
    Token,
)
from .support import Versions
from .workload import Boundary, Resource

type Outcome = Literal[
    "succeeded", "failed", "rejected", "cancelled", "unknown", "timed_out"
]
type Population = Literal[
    "all_submissions", "launched_cli", "readiness_attempts", "faults"
]


class Outcomes(Contract):
    """Mutually exclusive outcomes; timeouts are not silently merged with success."""

    succeeded: Count
    failed: Count
    rejected: Count
    cancelled: Count
    unknown: Count
    timed_out: Count

    @property
    def accounted(self) -> int:
        """Return the complete terminal outcome denominator."""
        return (
            self.succeeded
            + self.failed
            + self.rejected
            + self.cancelled
            + self.unknown
            + self.timed_out
        )


class Populations(Contract):
    """Declared sample populations; these are not resident-resource counts."""

    all_submissions: Count
    launched_cli: Count
    readiness_attempts: Count
    faults: Count

    def count(self, population: Population) -> int:
        """Return the named measurement population."""
        match population:
            case "all_submissions":
                return self.all_submissions
            case "launched_cli":
                return self.launched_cli
            case "readiness_attempts":
                return self.readiness_attempts
            case "faults":
                return self.faults

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject subpopulations larger than their parent populations."""
        if not (
            self.readiness_attempts <= self.launched_cli <= self.all_submissions
            and self.faults <= self.launched_cli
        ):
            raise PydanticCustomError("population_bounds", "population_bounds")
        return self


class Sample(Contract):
    """One submission's outcome with optional seconds, exclusion and reason code."""

    submission: PositiveCount
    outcome: Outcome
    seconds: Amount | None
    excluded: bool
    reason: Literal[
        "observed", "not_launched", "unavailable", "collection_failed", "excluded"
    ]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Keep reason codes consistent with measurement and exclusion state."""
        match self.reason:
            case "excluded":
                valid = self.excluded
            case "unavailable" | "collection_failed":
                valid = self.seconds is None and not self.excluded
            case "observed":
                valid = self.seconds is not None and not self.excluded
            case "not_launched":
                valid = not self.excluded
            case unreachable:
                assert_never(unreachable)
        if not valid:
            raise PydanticCustomError("sample_state", "sample_state")
        return self


class LatencyEvidence(Contract):
    """Full latency samples, not a success-only percentile summary."""

    boundary: Boundary
    unit: Literal["seconds"]
    population: Population
    samples: tuple[Sample, ...]

    @model_validator(mode="after")
    def unique_submissions(self) -> Self:
        """Reject two samples for one submission within a boundary."""
        if len({sample.submission for sample in self.samples}) != len(self.samples):
            raise PydanticCustomError("sample_link", "sample_link")
        return self


class HighWater(Contract):
    """An unavailable high-water observation remains null, never zero-filled."""

    resource: Resource
    value: Amount | None


class RunResult(Contract):
    """Self-declared observations; no offline authenticity or certification claim."""

    schema_version: SchemaVersion
    provenance: Literal["synthetic", "operator_declared"]
    manifest_revision: Digest
    support_row: Token
    observed_versions: Versions | None
    authorization_record: Token | None
    run_namespace: Token
    endpoint_aliases: Nonempty[Token]
    populations: Populations
    outcomes: Outcomes
    latency: tuple[LatencyEvidence, ...]
    high_water: tuple[HighWater, ...]
    elapsed_seconds: Amount | None
    stability_window_seconds: Amount | None
    stable_observations: Count | None
    early_stopped: bool
    missing_evidence: tuple[Token, ...]
    excluded_evidence: tuple[Token, ...]
    isolation_evidence_ref: Token | None
    artifact_receipt_ref: Token | None
    cleanup_receipt_ref: Token | None
    residual_assets: tuple[Token, ...]

    @field_validator("latency")
    @classmethod
    def linked(
        cls, value: tuple[LatencyEvidence, ...], info: ValidationInfo
    ) -> tuple[LatencyEvidence, ...]:
        """Reject sample links beyond the declared submission population."""
        populations = info.data.get("populations")
        if isinstance(populations, Populations) and any(
            sample.submission > populations.all_submissions
            for item in value
            for sample in item.samples
        ):
            raise PydanticCustomError("sample_link", "sample_link")
        return value

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject impossible denominators, unknown samples and duplicate metrics."""
        if self.observed_versions is not None and not self.observed_versions.resolved:
            raise PydanticCustomError(
                "observations_unresolved", "observations_unresolved"
            )
        if (
            self.elapsed_seconds is not None
            and self.stability_window_seconds is not None
            and self.stability_window_seconds > self.elapsed_seconds
        ):
            raise PydanticCustomError("stability_bounds", "stability_bounds")
        if (
            self.outcomes.accounted > self.populations.all_submissions
            or len({item.boundary for item in self.latency}) != len(self.latency)
            or len({item.resource for item in self.high_water}) != len(self.high_water)
            or any(
                len(item.samples) > self.populations.count(item.population)
                for item in self.latency
            )
        ):
            raise PydanticCustomError("result_bounds", "result_bounds")
        return self
