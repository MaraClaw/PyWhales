"""Simulation artifacts with explicit collection boundaries and ownership."""

from typing import Literal, Self, assert_never

from pydantic import field_validator, model_validator
from pydantic_core import PydanticCustomError

from scripts.fleet_contract.common import (
    Amount,
    Contract,
    Count,
    Digest,
    Positive,
    Token,
)
from scripts.fleet_contract.platform import Platform, Topology
from scripts.fleet_contract.results import (
    HighWater,
    LatencyEvidence,
    Outcome,
    Outcomes,
    Population,
    Populations,
    RunResult,
    Sample,
)
from scripts.fleet_contract.workload import (
    Artifact,
    Boundary,
    Image,
    Resource,
    Workload,
)

from .models import Metric, RunnableManifest, Step


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


class ExportRefused(Exception):
    """Fixed, input-independent refusal to export a simulation."""


def export_result(report: RunReport, manifest: RunnableManifest) -> RunResult:
    """Convert a fake run to synthetic evidence without inventing receipts."""
    if (
        manifest.provenance != "synthetic"
        or report.status == "rejected"
        or report.manifest_revision != manifest.identity.revision
        or report.workload_digest != manifest.identity.workload_digest
        or report.workload != manifest.workload
    ):
        raise ExportRefused
    boundaries: tuple[tuple[Boundary, Population], ...] = (
        ("queue_wait", "all_submissions"),
        ("selected_cli", "launched_cli"),
        ("readiness", "readiness_attempts"),
        ("recovery", "faults"),
    )
    resources: tuple[Resource, ...] = (
        "cpu_cores",
        "rss_bytes",
        "file_descriptors",
        "children",
        "disk_bytes",
        "disk_inodes",
    )
    latency = tuple(
        LatencyEvidence(
            boundary=boundary,
            unit="seconds",
            population=population,
            samples=tuple(
                Sample(
                    submission=sample.submission,
                    outcome=sample.outcome,
                    seconds=sample.seconds,
                    excluded=False,
                    reason=(
                        "unavailable"
                        if sample.reason == "unavailable"
                        else "observed"
                        if sample.seconds is not None
                        else "collection_failed"
                    ),
                )
                for sample in report.samples
                if sample.boundary == boundary
            ),
        )
        for boundary, population in boundaries
    )
    counts = {item.boundary: len(item.samples) for item in latency}
    return RunResult(
        schema_version=1,
        provenance="synthetic",
        manifest_revision=report.manifest_revision,
        support_row=manifest.support_row,
        observed_versions=None,
        authorization_record=None,
        run_namespace=manifest.authorization.run_namespace,
        endpoint_aliases=manifest.workload.endpoint_aliases,
        populations=Populations(
            all_submissions=report.submitted,
            launched_cli=counts["selected_cli"],
            readiness_attempts=counts["readiness"],
            faults=counts["recovery"],
        ),
        outcomes=report.outcomes,
        latency=latency,
        high_water=tuple(
            HighWater(
                resource=resource,
                value=next(
                    (
                        metric.value
                        for metric in report.metrics
                        if metric.name == resource
                    ),
                    None,
                ),
            )
            for resource in resources
        ),
        elapsed_seconds=report.load_elapsed_seconds,
        stability_window_seconds=None,
        stable_observations=None,
        early_stopped=report.status == "stopped",
        missing_evidence=("environment_unobserved", "stability_unobserved"),
        excluded_evidence=(),
        isolation_evidence_ref=None,
        artifact_receipt_ref=None,
        cleanup_receipt_ref=None,
        residual_assets=tuple(resource.slot for resource in report.cleanup.residual),
    )
