"""Event-driven admission for plans whose steps carry arrival offsets.

Submissions arrive at their scripted offsets and are admitted FIFO under both
global and per-action concurrency limits. Admitted operations advance through
real fake phases, so resources, command counts and cleanup change in
chronological order; no timestamp is shifted to imitate overlap.

Tie ordering. Events sharing a timestamp run in this order, then by submission:
1. phase ends of active operations (a final one is terminal and frees its slot);
2. lease or deadline expiries of queued submissions (deadlines start at arrival);
3. arrivals, in plan order;
4. retries, in submission order;
5. admissions, FIFO, after everything above at that instant.

A terminal event that stops the load cancels submissions still queued (even
those expiring at that instant) and keeps later arrivals from arriving; active
operations drain through their phases, each bounded by its own deadline, before
cleanup. Arrivals at or beyond the load deadline are never submitted.
"""

import heapq
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Final, Literal, assert_never

from scripts.fleet_contract.results import Outcome
from scripts.fleet_contract.workload import Action

from .fake import FakeDriver, Launch, Operation
from .models import (
    Plan,
    RunnableManifest,
    Step,
    deadline_reasons,
    exhausted,
    load_deadline,
    metrics_for,
    resource_reasons,
)
from .records import (
    Load,
    Observation,
    Peaks,
    QueueAttempt,
    QueueBackoff,
    Receipt,
    Schedule,
)
from .slots import AssetLimit, Slots

type Kind = Literal["phase", "expire", "arrive", "retry", "admit"]

RANK: Final[dict[Kind, int]] = {
    "phase": 0,
    "expire": 1,
    "arrive": 2,
    "retry": 3,
    "admit": 4,
}


@dataclass(slots=True, eq=False)
class Submission:
    """One scripted arrival; `queued` holds from arrival until admission or lapse."""

    number: int
    step: Step
    arrival: float
    deadline: float
    queued: bool = False
    pending_retry: bool = False
    enqueued: float = 0.0
    attempts: list[QueueAttempt] = field(default_factory=list)
    backoffs: list[QueueBackoff] = field(default_factory=list)


class Simulation:
    """Mutable state of one scheduled load: queue, slots, receipts and stop flag."""

    def __init__(
        self, manifest: RunnableManifest, plan: Plan, driver: FakeDriver
    ) -> None:
        """Bind the declarations and the driver; nothing is scheduled yet."""
        self.manifest, self.plan, self.driver = manifest, plan, driver
        self.slots = Slots(manifest)
        self.metrics = metrics_for(plan)
        self.end = load_deadline(manifest)
        self.limit = manifest.workload.concurrent_submissions
        self.action_limits = {
            op.action: op.concurrency for op in manifest.workload.operation_mix
        }
        self.action_active: defaultdict[Action, int] = defaultdict(int)
        self.action_peaks: dict[Action, int] = {}
        self.events: list[tuple[float, int, int, Kind]] = []
        self.queue: deque[Submission] = deque()
        self.submissions: list[Submission] = []
        self.operations: dict[int, Operation] = {}
        self.samples: dict[int, tuple[Observation, ...]] = {}
        self.receipts: dict[int, Receipt] = {}
        self.reasons: list[str] = []
        self.active = self.settled = self.failures = self.unknown = 0
        self.peak_active = self.peak_queued = 0
        self.stopping = self.waking = self.running_to_deadline = False

    def push(self, time: float, kind: Kind, number: int = 0) -> None:
        """Queue an event; the heap key is (time, rank, submission)."""
        heapq.heappush(self.events, (time, RANK[kind], number, kind))

    def stale(self, kind: Kind, number: int) -> bool:
        """Report an event that no longer happens: an expiry after admission or stop,
        or an arrival after the stop. Such events never move the fake clock.
        """
        submission = self.submissions[number - 1]
        return (
            (kind == "expire" and not (submission.queued or submission.pending_retry))
            or (kind == "retry" and not submission.pending_retry)
            or (kind in {"arrive", "retry"} and self.stopping)
        )

    def observe(self) -> None:
        """Fold the settled counts of the instant just finished into the peaks."""
        self.peak_active = max(self.peak_active, self.active)
        self.peak_queued = max(self.peak_queued, len(self.queue))
        for action, count in self.action_active.items():
            self.action_peaks[action] = max(self.action_peaks.get(action, 0), count)

    def wake(self) -> None:
        """Request one admission pass at this instant when a slot and a waiter exist."""
        if (
            self.queue
            and self.active < self.limit
            and self.action_active.get(self.queue[0].step.action, 0)
            < self.action_limits[self.queue[0].step.action]
            and not self.stopping
            and not self.waking
        ):
            self.waking = True
            self.push(self.driver.now, "admit")

    def record(self, receipt: Receipt, observed: tuple[Observation, ...]) -> None:
        """Keep a submission's receipt and samples under its submission number."""
        self.receipts[receipt.submission] = receipt
        self.samples[receipt.submission] = observed

    def lapse(self, submission: Submission, outcome: Outcome) -> None:
        """Terminate a submission that never launched, at the current instant."""
        now = self.driver.now
        if submission.queued:
            submission.attempts.append(
                QueueAttempt(enqueued_seconds=submission.enqueued, ended_seconds=now)
            )
        if (
            submission.pending_retry
            and submission.step.queue_retry_backoff_seconds is not None
        ):
            submission.backoffs.append(
                QueueBackoff(
                    started_seconds=submission.attempts[-1].ended_seconds,
                    ended_seconds=now,
                )
            )
        submission.queued = submission.pending_retry = False
        launch = Launch(
            submission=submission.number,
            deadline=submission.deadline,
            resource=None,
            arrival=submission.arrival,
            queue=now - submission.arrival,
        )
        receipt = Receipt(
            submission=submission.number,
            action=submission.step.action,
            arrival_seconds=submission.arrival,
            admitted_seconds=None,
            terminal_seconds=now,
            outcome=outcome,
            attempts=tuple(submission.attempts)
            if submission.step.queue_lease_seconds is not None
            else None,
            backoffs=tuple(submission.backoffs)
            if submission.step.queue_retry_backoff_seconds is not None
            else None,
        )
        self.record(receipt, self.driver.lapse(submission.step, launch, outcome))

    def stop(self) -> None:
        """Stop the load: cancel queued work; active operations keep draining."""
        self.stopping = True
        while self.queue:
            self.lapse(self.queue.popleft(), "cancelled")
        for submission in self.submissions:
            if submission.pending_retry:
                self.lapse(submission, "cancelled")

    def settle(self, outcome: Outcome) -> None:
        """Evaluate early-stop rules after one terminal event, as the sequential loop does."""
        if self.stopping:
            return
        budgets = self.manifest.budgets
        self.settled += 1
        self.failures += outcome != "succeeded"
        self.unknown += outcome == "unknown"
        reasons = resource_reasons(self.manifest, self.metrics)
        if self.unknown and self.unknown >= budgets.early_stop_unknown_count:
            reasons.append("unknown_limit")
        if (
            self.driver.now < self.end
            and self.failures
            and self.failures / self.settled >= budgets.early_stop_error_fraction
        ):
            reasons.append("failure_limit")
        if reasons:
            self.reasons.extend(reasons)
            self.stop()

    def arrive(self, submission: Submission) -> None:
        """Queue an arrival."""
        submission.queued = True
        submission.pending_retry = False
        submission.enqueued = self.driver.now
        self.queue.append(submission)
        lease = submission.step.queue_lease_seconds
        expiry = (
            min(submission.deadline, self.driver.now + lease)
            if lease is not None
            else submission.deadline
        )
        self.push(expiry, "expire", submission.number)

    def expire(self, submission: Submission) -> None:
        """Expire an attempt, retrying only before the original deadline."""
        if submission.pending_retry:
            self.lapse(submission, "timed_out")
            self.settle("timed_out")
            return
        self.queue.remove(submission)
        if self.driver.now < submission.deadline and len(submission.attempts) + 1 < (
            submission.step.max_queue_attempts or 1
        ):
            submission.attempts.append(
                QueueAttempt(
                    enqueued_seconds=submission.enqueued, ended_seconds=self.driver.now
                )
            )
            submission.queued = False
            submission.pending_retry = True
            eligibility = self.driver.now + (
                submission.step.queue_retry_backoff_seconds or 0.0
            )
            self.push(
                min(submission.deadline, eligibility),
                "retry" if eligibility < submission.deadline else "expire",
                submission.number,
            )
            return
        self.lapse(submission, "timed_out")
        self.settle("timed_out")

    def retry(self, submission: Submission) -> None:
        """End pending backoff at eligibility before the original deadline."""
        if submission.step.queue_retry_backoff_seconds is not None:
            submission.backoffs.append(
                QueueBackoff(
                    started_seconds=submission.attempts[-1].ended_seconds,
                    ended_seconds=self.driver.now,
                )
            )
        self.arrive(submission)

    def admit(self) -> None:
        """Admit queued submissions FIFO into free slots at the current instant."""
        self.waking = False
        driver = self.driver
        while self.queue and self.active < self.limit and not self.stopping:
            submission = self.queue[0]
            action = submission.step.action
            if self.action_active.get(action, 0) >= self.action_limits[action]:
                break
            try:
                resource = self.slots.resolve(
                    submission.step, submission.number, driver
                )
            except AssetLimit:
                self.reasons.append("asset_limit")
                self.stop()
                return
            self.queue.popleft()
            submission.attempts.append(
                QueueAttempt(
                    enqueued_seconds=submission.enqueued, ended_seconds=driver.now
                )
            )
            submission.queued = False
            launch = Launch(
                submission=submission.number,
                deadline=submission.deadline,
                resource=resource,
                arrival=submission.arrival,
                queue=driver.now - submission.arrival,
            )
            operation = driver.begin(submission.step, launch)
            self.operations[submission.number] = operation
            self.active += 1
            self.action_active[action] += 1
            self.push(operation.phases[0][1], "phase", submission.number)

    def phase(self, number: int) -> None:
        """Enter an operation's next phase, or settle it after its final phase end."""
        operation = self.operations[number]
        following = self.driver.advance_phase(operation)
        if following is not None:
            self.push(following, "phase", number)
            return
        self.active -= 1
        self.action_active[operation.step.action] -= 1
        observed = self.driver.finish(operation)
        outcome = observed[0].outcome
        receipt = Receipt(
            submission=number,
            action=operation.step.action,
            arrival_seconds=operation.launch.arrival,
            admitted_seconds=operation.admitted,
            terminal_seconds=operation.end,
            outcome=outcome,
            attempts=tuple(self.submissions[number - 1].attempts)
            if operation.step.queue_lease_seconds is not None
            else None,
            backoffs=tuple(self.submissions[number - 1].backoffs)
            if operation.step.queue_retry_backoff_seconds is not None
            else None,
        )
        self.record(receipt, observed)
        self.settle(outcome)

    def run(self) -> Load:
        """Replay every event in order, then apply the sequential termination rules."""
        limit = (
            self.manifest.workload.termination_operations
            if self.manifest.workload.termination == "operation_count"
            else None
        )
        scripted = zip(self.plan.steps[:limit], self.plan.offsets[:limit], strict=True)
        for number, (step, arrival) in enumerate(scripted, start=1):
            if arrival >= self.end:
                self.running_to_deadline = True
                break
            deadline = min(self.end, arrival + self.plan.operation_timeout_seconds)
            self.submissions.append(Submission(number, step, arrival, deadline))
            self.push(arrival, "arrive", number)
        instant: float | None = None
        while self.events:
            time, _, number, kind = heapq.heappop(self.events)
            if kind in {"expire", "arrive", "retry"} and self.stale(kind, number):
                continue
            if instant is not None and time > instant:
                self.observe()
            self.driver.now = instant = time
            match kind:
                case "phase":
                    self.phase(number)
                case "expire":
                    self.expire(self.submissions[number - 1])
                case "arrive":
                    self.arrive(self.submissions[number - 1])
                case "retry":
                    self.retry(self.submissions[number - 1])
                case "admit":
                    self.admit()
                case unreachable:
                    assert_never(unreachable)
            self.wake()
        self.observe()
        return self.result()

    def result(self) -> Load:
        """Apply deadline and exhaustion reasons and freeze receipts in submission order."""
        driver, submitted = self.driver, len(self.receipts)
        if self.running_to_deadline and not self.stopping:
            driver.now = max(driver.now, self.end)
        if not self.reasons and driver.now >= self.end:
            self.reasons.extend(deadline_reasons(self.manifest))
        if not self.reasons and exhausted(self.manifest, driver.now, submitted):
            self.reasons.append("plan_exhausted")
        order = sorted(self.receipts)
        return Load(
            samples=tuple(item for number in order for item in self.samples[number]),
            submitted=submitted,
            reasons=tuple(self.reasons),
            schedule=Schedule(
                version=2
                if any(
                    s.queue_retry_backoff_seconds is not None for s in self.plan.steps
                )
                else 1,
                peaks=Peaks(
                    active=self.peak_active,
                    queued=self.peak_queued,
                    per_action=tuple(sorted(self.action_peaks.items())),
                ),
                receipts=tuple(self.receipts[number] for number in order),
            ),
        )
