"""Deterministic in-memory driver; never imports a process or network adapter."""

from dataclasses import dataclass
from typing import Literal, assert_never

from scripts.fleet_contract.results import Outcome
from scripts.fleet_contract.workload import Boundary

from .models import Plan, Step
from .records import Cleanup, Observation, OwnedResource

type Phase = Literal["local", "readiness", "recovery", "done"]
type Value = tuple[Boundary, float | None, float, float]


@dataclass(frozen=True, slots=True)
class Launch:
    """Per-submission identity, arrival and wait; `queue` is the wait before launch."""

    submission: int
    deadline: float
    resource: OwnedResource | None
    arrival: float
    queue: float


@dataclass(slots=True)
class Operation:
    """A launched submission whose fake-clock phase ends are replayed as events."""

    step: Step
    launch: Launch
    admitted: float
    outcome: Outcome
    phases: tuple[tuple[Phase, float], ...]
    local: float
    ready: float | None
    recovery: float | None
    position: int = 0

    @property
    def phase(self) -> Phase:
        """Return the phase in progress, or `done` after the final phase end."""
        return (
            self.phases[self.position][0]
            if self.position < len(self.phases)
            else "done"
        )

    @property
    def end(self) -> float:
        """Return the fake time of the final phase end (never past the deadline)."""
        return self.phases[-1][1]


class FakeDriver:
    """Mutable fake clock and resource inventory owned by one simulation run."""

    def __init__(self) -> None:
        """Start with an empty inventory and zero calls; contact nothing."""
        self.now = 0.0
        self.calls = 0
        self.commands = 0
        self.ledger: list[OwnedResource] = []
        self.live: list[OwnedResource] = []

    def advance(self, seconds: float, deadline: float) -> float:
        """Consume only remaining simulated time, including exact deadline edges."""
        elapsed = min(seconds, max(0.0, deadline - self.now))
        self.now += elapsed
        return elapsed

    def begin(self, step: Step, launch: Launch) -> Operation:
        """Launch at the current fake time: count the command, apply creation, plan phases.

        Created identities are retained even when the response is later lost.
        """
        self.calls += 1
        self.commands += 1
        outcome: Outcome = step.outcome
        resource = launch.resource
        match step.action:
            case "create" | "pull" | "build":
                if resource is not None:
                    self.ledger.append(resource)
                    self.live.append(resource)
            case "start" | "stop" | "remove":
                if resource not in self.live:
                    outcome = "failed"
            case "inspect" | "fault":
                pass
            case "cleanup":
                raise AssertionError("reserved_cleanup")
            case unreachable:
                assert_never(unreachable)
        admitted = at = self.now
        phases: list[tuple[Phase, float]] = []
        spans: list[float] = []
        scripted: tuple[tuple[Phase, float | None], ...] = (
            ("local", step.local_seconds),
            ("readiness", step.readiness_seconds),
            ("recovery", step.recovery_seconds),
        )
        for phase, seconds in scripted:
            if seconds is None:
                continue
            span = min(seconds, max(0.0, launch.deadline - at))
            at += span
            phases.append((phase, at))
            spans.append(span)
        return Operation(
            step=step,
            launch=launch,
            admitted=admitted,
            outcome=outcome,
            phases=tuple(phases),
            local=spans[0],
            ready=None
            if step.readiness_seconds is None
            else phases[1][1] - launch.arrival,
            recovery=None
            if step.recovery_seconds is None
            else phases[-1][1] - admitted,
        )

    def advance_phase(self, operation: Operation) -> float | None:
        """Enter the next phase at its event time; return its end, or None when done."""
        operation.position += 1
        if operation.position < len(operation.phases):
            return operation.phases[operation.position][1]
        return None

    def finish(self, operation: Operation) -> tuple[Observation, ...]:
        """Settle a drained operation: commit a removal in time, then record samples."""
        step, launch, end = operation.step, operation.launch, operation.end
        outcome = operation.outcome
        if step.action == "remove" and outcome == "succeeded" and end < launch.deadline:
            if launch.resource is not None and launch.resource in self.live:
                self.live.remove(launch.resource)
            else:
                outcome = "failed"
        if end >= launch.deadline:
            outcome = "timed_out"
        start, queue_end = launch.arrival, operation.admitted
        values: list[Value] = [
            ("queue_wait", launch.queue, start, queue_end),
            ("selected_cli", operation.local, queue_end, queue_end + operation.local),
            (
                "readiness",
                operation.ready,
                start,
                queue_end + operation.local
                if operation.ready is None
                else start + operation.ready,
            ),
        ]
        if step.action == "fault":
            values.append(
                ("recovery", operation.recovery, end - (operation.recovery or 0.0), end)
            )
        return observations(step, launch, outcome, values)

    def lapse(
        self, step: Step, launch: Launch, outcome: Outcome
    ) -> tuple[Observation, ...]:
        """Record a submission that never launched: only its wait until now."""
        return observations(
            step,
            launch,
            outcome,
            [("queue_wait", launch.queue, launch.arrival, self.now)],
        )

    def execute(self, step: Step, launch: Launch) -> tuple[Observation, ...]:
        """Run one submission to completion on the sequential clock.

        The runner has already consumed any scripted queue wait; a wait that
        exhausts the deadline launches nothing.
        """
        if self.now >= launch.deadline:
            return self.lapse(step, launch, "timed_out")
        operation = self.begin(step, launch)
        self.now = operation.end
        return self.finish(operation)

    def cleanup(self, plan: Plan, seconds: float) -> Cleanup:
        """Remove only exact acquired live identities within one cleanup budget."""
        start = self.now
        deadline = start + seconds
        attempted: list[OwnedResource] = []
        removed: list[OwnedResource] = []
        for resource in reversed(self.live.copy()):
            if self.now >= deadline:
                break
            attempted.append(resource)
            self.calls += 1
            spent = self.advance(plan.cleanup_seconds_per_asset, deadline)
            if (
                spent < plan.cleanup_seconds_per_asset
                or resource.slot in plan.cleanup_fail_assets
            ):
                continue
            self.live.remove(resource)
            removed.append(resource)
        return Cleanup(
            attempted=tuple(attempted),
            removed=tuple(removed),
            residual=tuple(self.live),
            elapsed_seconds=self.now - start,
            timed_out=bool(self.live) and self.now >= deadline,
        )


def observations(
    step: Step, launch: Launch, outcome: Outcome, values: list[Value]
) -> tuple[Observation, ...]:
    """Build one terminal observation per boundary; unavailable seconds stay null."""
    reason = (
        "deadline"
        if outcome == "timed_out"
        else "operation_failed"
        if outcome != "succeeded"
        else "observed"
    )
    return tuple(
        Observation(
            submission=launch.submission,
            scenario=step.scenario,
            boundary=boundary,
            outcome=outcome,
            seconds=seconds,
            reason="unavailable" if seconds is None else reason,
            started_at_seconds=begin,
            ended_at_seconds=end,
        )
        for boundary, seconds, begin, end in values
    )
