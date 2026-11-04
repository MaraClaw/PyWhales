"""Strict simulation inputs and resolved contributor declarations."""

from collections.abc import Sequence
from typing import Final, Literal, Self, assert_never

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from scripts.fleet_contract.common import (
    Amount,
    Contract,
    Decision,
    Nonempty,
    Positive,
    PositiveCount,
    SchemaVersion,
    Token,
)
from scripts.fleet_contract.manifest import Manifest
from scripts.fleet_contract.platform import Ownership, Platform, Topology
from scripts.fleet_contract.results import Outcome
from scripts.fleet_contract.safety import Authorization, Budgets
from scripts.fleet_contract.workload import Action, Identity, Measurements, Workload


class RunnableManifest(Contract):
    """Fully resolved declarations; never proof of permission or isolation."""

    schema_version: SchemaVersion
    state: Literal["runnable"]
    provenance: Literal["synthetic", "operator_declared"]
    identity: Identity
    support_row: Token
    workload: Workload
    platform: Platform
    topology: Topology
    ownership: Ownership
    measurements: Measurements
    budgets: Budgets
    authorization: Authorization
    decisions: tuple[Decision, ...]

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        """Run the authoritative cross-group validators without narrowing unions."""
        Manifest.model_validate_json(self.model_dump_json())
        return self


class QueueRetryExponential(Contract):
    """Capped doubling delay after each expired queue attempt, in fake-clock seconds."""

    initial_seconds: Positive
    max_seconds: Positive

    @model_validator(mode="after")
    def capped(self) -> Self:
        """Require a cap that admits the first delay."""
        if self.max_seconds < self.initial_seconds:
            raise PydanticCustomError("exponential_cap", "exponential_cap")
        return self

    def delay(self, attempt: int) -> float:
        """Return `min(max, initial * 2**(attempt - 1))` by capped doubling.

        The cap is reached within a few thousand doublings of any finite float,
        so a large `attempt` neither loops long nor overflows.
        """
        delay = self.initial_seconds
        for _ in range(attempt - 1):
            if delay >= self.max_seconds - delay:
                return self.max_seconds
            delay *= 2
        return delay


class Step(Contract):
    """One scripted fake operation; timings are seconds on the fake clock.

    `arrival_seconds` opts a plan into overlapping execution: the submission
    arrives at that offset and waits for admission, so a scripted `queue_seconds`
    would double-count the wait and is rejected beside it.
    """

    scenario: Token
    endpoint: Token
    action: Action
    asset: Token | None = None
    fault: Token | None = None
    outcome: Outcome = "succeeded"
    arrival_seconds: Amount | None = None
    queue_lease_seconds: Positive | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    max_queue_attempts: PositiveCount | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    queue_retry_backoff_seconds: Positive | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    queue_retry_exponential: QueueRetryExponential | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    queue_seconds: Amount = 0.0
    local_seconds: Amount = 0.1
    readiness_seconds: Amount | None = 0.1
    recovery_seconds: Amount | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Require explicit fault identity and allocation slots for mutations."""
        if (self.action == "fault") != (self.fault is not None):
            raise PydanticCustomError("fault_identity", "fault_identity")
        if (self.action in {"create", "pull", "build", "start", "stop", "remove"}) != (
            self.asset is not None
        ):
            raise PydanticCustomError("asset_identity", "asset_identity")
        if self.action == "cleanup":
            raise PydanticCustomError("cleanup_reserved", "cleanup_reserved")
        if self.action != "fault" and self.recovery_seconds is not None:
            raise PydanticCustomError("recovery_population", "recovery_population")
        if self.arrival_seconds is not None and self.queue_seconds:
            raise PydanticCustomError("scheduled_queue", "scheduled_queue")
        if self.arrival_seconds is None and (
            self.queue_lease_seconds is not None
            or self.max_queue_attempts is not None
            or self.delayed
        ):
            raise PydanticCustomError("lease_unscheduled", "lease_unscheduled")
        if (self.max_queue_attempts or 1) > 1 and self.queue_lease_seconds is None:
            raise PydanticCustomError("lease_required", "lease_required")
        if (
            self.queue_retry_backoff_seconds is not None
            and self.queue_retry_exponential is not None
        ):
            raise PydanticCustomError(
                "retry_policy_exclusive", "retry_policy_exclusive"
            )
        if self.delayed and (
            self.queue_lease_seconds is None or (self.max_queue_attempts or 1) < 2
        ):
            raise PydanticCustomError(
                "backoff_requires_retry", "backoff_requires_retry"
            )
        return self

    @property
    def delayed(self) -> bool:
        """Report whether an expired queue attempt waits before its next attempt."""
        return (
            self.queue_retry_backoff_seconds is not None
            or self.queue_retry_exponential is not None
        )

    def retry_delay(self, attempt: int) -> float:
        """Return the wait after queued attempt number `attempt` expires; zero if immediate."""
        if self.queue_retry_exponential is not None:
            return self.queue_retry_exponential.delay(attempt)
        return self.queue_retry_backoff_seconds or 0.0


type MetricName = Literal[
    "cpu_cores",
    "rss_bytes",
    "file_descriptors",
    "children",
    "disk_bytes",
    "disk_inodes",
    "network_bytes",
    "daemon_cpu_cores",
    "daemon_memory_bytes",
]

METRIC_NAMES: Final[tuple[MetricName, ...]] = (
    "cpu_cores",
    "rss_bytes",
    "file_descriptors",
    "children",
    "disk_bytes",
    "disk_inodes",
    "network_bytes",
    "daemon_cpu_cores",
    "daemon_memory_bytes",
)


class Metric(Contract):
    """A scripted resource high-water; null explicitly means unavailable."""

    name: MetricName
    value: Amount | None


class Plan(Contract):
    """Bounded simulation recipe, not a real adapter configuration."""

    provider: Literal["fake", "real"] = "fake"
    opt_in: bool = False
    repetitions: PositiveCount
    maximum_samples: PositiveCount
    operation_timeout_seconds: Positive
    steps: Nonempty[Step]
    metrics: tuple[Metric, ...] = ()
    cleanup_seconds_per_asset: Amount = 0.1
    cleanup_fail_assets: tuple[Token, ...] = ()

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """Bound memory by an explicit sample cap and reject duplicate metrics."""
        if (
            sum(3 + (step.action == "fault") for step in self.steps) * self.repetitions
            > self.maximum_samples
        ):
            raise PydanticCustomError("sample_cap", "sample_cap")
        if len({metric.name for metric in self.metrics}) != len(self.metrics):
            raise PydanticCustomError("duplicate_metric", "duplicate_metric")
        return self

    @model_validator(mode="after")
    def arrivals(self) -> Self:
        """Require all-or-none nondecreasing offsets and one repetition when scheduled."""
        offsets = tuple(step.arrival_seconds for step in self.steps)
        present = tuple(offset for offset in offsets if offset is not None)
        if not present:
            return self
        if len(present) != len(offsets):
            raise PydanticCustomError("arrival_incomplete", "arrival_incomplete")
        if self.repetitions != 1:
            raise PydanticCustomError("arrival_repetitions", "arrival_repetitions")
        if present != tuple(sorted(present)):
            raise PydanticCustomError("arrival_order", "arrival_order")
        return self

    @property
    def offsets(self) -> tuple[float, ...]:
        """Return every arrival offset of a scheduled plan; empty when sequential."""
        return tuple(
            step.arrival_seconds
            for step in self.steps
            if step.arrival_seconds is not None
        )

    @property
    def scheduled(self) -> bool:
        """Report whether the plan overlaps submissions (all steps, or none, have offsets)."""
        return bool(self.offsets)


def schedule_version(steps: Sequence[Step]) -> Literal[1, 2, 3]:
    """Return the local schedule version a plan's retry policies require."""
    if any(step.queue_retry_exponential is not None for step in steps):
        return 3
    if any(step.queue_retry_backoff_seconds is not None for step in steps):
        return 2
    return 1


def load_deadline(manifest: RunnableManifest) -> float:
    """Return the fake-clock limit shared by the sequential and scheduled loads."""
    return min(
        manifest.authorization.maximum_run_seconds,
        manifest.authorization.expires_after_seconds,
        manifest.budgets.early_stop_duration_seconds,
        manifest.workload.duration_seconds,
    )


def deadline_reasons(manifest: RunnableManifest) -> tuple[str, ...]:
    """Return the stop reason when the load deadline ends an operation-count target."""
    match manifest.workload.termination:
        case "duration":
            return ()
        case "operation_count":
            return ("duration_limit",)
        case unreachable:
            assert_never(unreachable)


def exhausted(manifest: RunnableManifest, now: float, submitted: int) -> bool:
    """Report whether a short script ended before the declared termination threshold."""
    match manifest.workload.termination:
        case "duration":
            return now < manifest.workload.duration_seconds
        case "operation_count":
            return submitted < (manifest.workload.termination_operations or 0)
        case unreachable:
            assert_never(unreachable)


def metrics_for(plan: Plan) -> tuple[Metric, ...]:
    """Return every known metric, null where the plan scripts no observation."""
    return tuple(
        Metric(
            name=name,
            value=next(
                (metric.value for metric in plan.metrics if metric.name == name), None
            ),
        )
        for name in METRIC_NAMES
    )


def resource_reasons(
    manifest: RunnableManifest, metrics: tuple[Metric, ...]
) -> list[str]:
    """Return the early-stop reasons the scripted metrics trigger after a submission."""
    reasons: list[str] = []
    for budget in manifest.budgets.resources:
        value = next(
            metric.value for metric in metrics if metric.name == budget.resource
        )
        if value is None:
            reasons.append("resource_metrics_unavailable")
        elif value >= budget.early_stop:
            reasons.append("resource_limit")
    return reasons


def authorize(manifest: RunnableManifest, plan: Plan) -> tuple[str, ...]:
    """Check every declaration before constructing or calling the fake provider."""
    reasons: list[str] = []
    if not plan.opt_in:
        reasons.append("opt_in_required")
    if plan.provider != "fake":
        reasons.append("real_adapter_unqualified")
    auth = manifest.authorization
    aliases = {
        alias.name: alias.effective_endpoint for alias in manifest.platform.aliases
    }
    removable = {
        asset.id
        for asset in manifest.ownership.assets
        if asset.ownership == "created"
        and asset.data == "ephemeral"
        and asset.teardown == "remove_owned"
    }
    declared = {
        operation.action
        for operation in manifest.workload.operation_mix
        if operation.fraction > 0
    }
    for step in plan.steps:
        if (
            step.endpoint not in manifest.workload.endpoint_aliases
            or aliases.get(step.endpoint) not in auth.effective_endpoint_allowlist
        ):
            reasons.append("endpoint_not_allowed")
        if step.action not in auth.allowed_actions or step.action not in declared:
            reasons.append("action_not_allowed")
        if step.fault is not None and step.fault not in auth.destructive_scenarios:
            reasons.append("fault_not_allowed")
        if step.asset is not None and step.asset not in removable:
            reasons.append("asset_not_owned_ephemeral")
    if not set(plan.cleanup_fail_assets) <= removable:
        reasons.append("cleanup_asset_unknown")
    return tuple(sorted(set(reasons)))
