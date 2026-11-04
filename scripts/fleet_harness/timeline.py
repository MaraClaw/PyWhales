"""Pure receipt arithmetic: settled peaks, FIFO order and report reconciliation.

Peaks are the largest counts *settled* after every event of an instant, over the
half-open spans `[arrival, admission)` (queued) and `[admission, terminal)`
(active). Settling makes them independent of the order of events that share a
timestamp, so the scheduler's live counters and a recount from receipts agree.
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Literal

from scripts.fleet_contract.workload import Action

from .models import Step, schedule_version

if TYPE_CHECKING:
    from .records import Observation, Receipt, RunReport

type Code = Literal["schedule_binding", "schedule_concurrency", "schedule_times"]

TOLERANCE: Final = 1e-9
BOUNDARY_ORDER: Final = {
    "queue_wait": 0,
    "selected_cli": 1,
    "readiness": 2,
    "recovery": 3,
}


def close(left: float, right: float) -> bool:
    """Compare fake-clock seconds within float noise."""
    return math.isclose(left, right, rel_tol=0.0, abs_tol=TOLERANCE)


def peak(deltas: dict[float, int]) -> int:
    """Return the largest running total settled after all deltas of each instant."""
    running = best = 0
    for instant in sorted(deltas):
        running += deltas[instant]
        best = max(best, running)
    return best


def peak_counts(receipts: Sequence[Receipt]) -> tuple[int, int]:
    """Recount the settled (active, queued) peaks from receipts alone."""
    active: defaultdict[float, int] = defaultdict(int)
    queued: defaultdict[float, int] = defaultdict(int)
    for receipt in receipts:
        spans = receipt.attempts
        if spans is None:
            queued[receipt.arrival_seconds] += 1
            queued[
                receipt.admitted_seconds
                if receipt.admitted_seconds is not None
                else receipt.terminal_seconds
            ] -= 1
        else:
            for span in spans:
                queued[span.enqueued_seconds] += 1
                queued[span.ended_seconds] -= 1
        if receipt.admitted_seconds is None:
            continue
        else:
            active[receipt.admitted_seconds] += 1
            active[receipt.terminal_seconds] -= 1
    return peak(active), peak(queued)


def action_peak_counts(receipts: Sequence[Receipt]) -> tuple[tuple[Action, int], ...]:
    """Recount settled active peaks for actions with launched submissions."""
    actions: dict[Action, defaultdict[float, int]] = {}
    for receipt in receipts:
        if receipt.admitted_seconds is not None:
            deltas = actions.setdefault(receipt.action, defaultdict(int))
            deltas[receipt.admitted_seconds] += 1
            deltas[receipt.terminal_seconds] -= 1
    return tuple(sorted((action, peak(deltas)) for action, deltas in actions.items()))


def is_ordered(receipts: Sequence[Receipt]) -> bool:
    """Check numbering, arrival and time order, and that admission is strictly FIFO.

    A launch may neither precede an earlier launch nor happen while an earlier
    submission was still waiting, so it waits for every earlier unlaunched
    terminal (`barrier`).
    """
    arrival = 0.0
    attempts: list[tuple[float, int, int, float, bool]] = []
    for number, receipt in enumerate(receipts, start=1):
        if (
            receipt.submission != number
            or receipt.arrival_seconds < arrival
            or receipt.terminal_seconds < receipt.arrival_seconds
        ):
            return False
        arrival = receipt.arrival_seconds
        launched = receipt.admitted_seconds
        end = launched if launched is not None else receipt.terminal_seconds
        spans = receipt.attempts
        if spans is None:
            if receipt.backoffs is not None:
                return False
            attempts.append((arrival, 0, number, end, launched is not None))
        else:
            delays = receipt.backoffs or ()
            pending = len(delays) == len(spans)
            if (
                not spans
                or spans[0].enqueued_seconds != arrival
                or (
                    receipt.backoffs is not None
                    and len(delays) not in {len(spans) - 1, len(spans)}
                )
                or (pending and launched is not None)
                or (
                    delays[-1].ended_seconds
                    if delays and pending
                    else spans[-1].ended_seconds
                )
                != end
            ):
                return False
            for index, delay in enumerate(delays):
                if (
                    delay.started_seconds != spans[index].ended_seconds
                    or delay.ended_seconds < delay.started_seconds
                    or delay.ended_seconds
                    != (
                        spans[index + 1].enqueued_seconds
                        if index + 1 < len(spans)
                        else end
                    )
                ):
                    return False
            for index, span in enumerate(spans):
                if span.enqueued_seconds > span.ended_seconds or (
                    index
                    and span.enqueued_seconds
                    != (
                        delays[index - 1].ended_seconds
                        if delays
                        else spans[index - 1].ended_seconds
                    )
                ):
                    return False
                attempts.append(
                    (
                        span.enqueued_seconds,
                        int(index > 0),
                        number,
                        span.ended_seconds,
                        launched is not None and index == len(spans) - 1,
                    )
                )
        if launched is None:
            if receipt.outcome not in {"timed_out", "cancelled"}:
                return False
        elif not arrival <= launched <= receipt.terminal_seconds:
            return False
    barrier = 0.0
    for _, _, _, end, admitted in sorted(attempts):
        if admitted and end < barrier:
            return False
        barrier = max(barrier, end)
    return True


def consistent(receipt: Receipt, samples: Sequence[Observation]) -> bool:
    """Check one receipt against the samples of its submission."""
    queue = samples[0]
    left = (
        receipt.terminal_seconds
        if receipt.admitted_seconds is None
        else receipt.admitted_seconds
    )
    if (
        queue.outcome != receipt.outcome
        or queue.seconds is None
        or not close(queue.seconds, left - receipt.arrival_seconds)
        or not close(queue.started_at_seconds, receipt.arrival_seconds)
        or not close(queue.ended_at_seconds, left)
        or any(
            sample.started_at_seconds < receipt.arrival_seconds - TOLERANCE
            or sample.ended_at_seconds > receipt.terminal_seconds + TOLERANCE
            for sample in samples
        )
        or not close(
            max(sample.ended_at_seconds for sample in samples), receipt.terminal_seconds
        )
    ):
        return False
    launches = [sample for sample in samples if sample.boundary == "selected_cli"]
    if receipt.admitted_seconds is None:
        return len(samples) == 1
    return len(launches) == 1 and close(
        launches[0].started_at_seconds, receipt.admitted_seconds
    )


def bound(report: RunReport) -> bool:
    """Check that receipts, scripted offsets, repetitions and status agree."""
    schedule = report.schedule
    if schedule is None:
        return all(step.arrival_seconds is None for step in report.steps)
    offsets = [step.arrival_seconds for step in report.steps]
    present = [offset for offset in offsets if offset is not None]
    declared = {operation.action for operation in report.workload.operation_mix}
    return (
        len(offsets) > 0
        and len(present) == len(offsets)
        and present == sorted(present)
        and report.repetitions == 1
        and schedule.version == schedule_version(report.steps)
        and len(schedule.receipts) == report.submitted <= len(offsets)
        and all(
            receipt.arrival_seconds == step.arrival_seconds
            and receipt.action == step.action
            and receipt.action in declared
            for receipt, step in zip(schedule.receipts, report.steps, strict=False)
        )
        and (
            report.status == "stopped"
            or not any(
                receipt.admitted_seconds is None and receipt.outcome == "cancelled"
                for receipt in schedule.receipts
            )
        )
    )


def timed(report: RunReport, receipts: Sequence[Receipt]) -> bool:
    """Check receipt times against the load window, deadline and canonical samples."""
    keys = [
        (sample.submission, BOUNDARY_ORDER[sample.boundary])
        for sample in report.samples
    ]
    by_submission: defaultdict[int, list[Observation]] = defaultdict(list)
    for sample in report.samples:
        by_submission[sample.submission].append(sample)
    window = report.workload.duration_seconds
    cancelled = [
        r.terminal_seconds
        for r in receipts
        if r.admitted_seconds is None and r.outcome == "cancelled"
    ]
    if cancelled:
        stop = cancelled[0]
        if (
            any(time != stop for time in cancelled)
            or any(
                r.admitted_seconds is not None and r.admitted_seconds > stop
                for r in receipts
            )
            or any(
                r.admitted_seconds is None and r.terminal_seconds > stop
                for r in receipts
            )
            or (
                "asset_limit" not in report.reasons
                and not any(
                    r.terminal_seconds == stop and r.outcome != "cancelled"
                    for r in receipts
                )
            )
        ):
            return False
    return keys == sorted(set(keys)) and all(
        receipt.arrival_seconds < window
        and receipt.terminal_seconds <= window + TOLERANCE
        and receipt.terminal_seconds
        <= receipt.arrival_seconds + report.operation_timeout_seconds + TOLERANCE
        and consistent(receipt, by_submission[receipt.submission])
        and lineage(
            receipt,
            step,
            min(window, receipt.arrival_seconds + report.operation_timeout_seconds),
        )
        for receipt, step in zip(receipts, report.steps, strict=False)
    )


def lineage(receipt: Receipt, step: Step, deadline: float) -> bool:
    """Bind alternating queue/backoff spans to leases, fixed delays and deadlines."""
    lease = step.queue_lease_seconds
    spans = receipt.attempts
    backoffs = receipt.backoffs
    if receipt.admitted_seconds is None and receipt.outcome not in {
        "timed_out",
        "cancelled",
    }:
        return False
    if step.delayed != (backoffs is not None):
        return False
    if lease is None:
        return spans is None and (
            receipt.admitted_seconds < deadline
            if receipt.admitted_seconds is not None
            else receipt.outcome != "timed_out"
            or close(receipt.terminal_seconds, deadline)
        )
    if spans is None or not spans or len(spans) > (step.max_queue_attempts or 1):
        return False
    pending = backoffs is not None and len(backoffs) == len(spans)
    if backoffs is not None:
        for index, span in enumerate(backoffs):
            eligibility = span.started_seconds + step.retry_delay(index + 1)
            if (
                span.started_seconds >= deadline
                or index + 1 >= (step.max_queue_attempts or 1)
                or span.ended_seconds > min(eligibility, deadline) + TOLERANCE
            ):
                return False
            if index + 1 < len(spans):
                if (
                    not close(span.ended_seconds, eligibility)
                    or span.ended_seconds >= deadline
                ):
                    return False
            elif receipt.outcome == "timed_out":
                if eligibility < deadline or not close(span.ended_seconds, deadline):
                    return False
            elif receipt.outcome != "cancelled":
                return False
    valid = all(
        span.ended_seconds <= span.enqueued_seconds + lease + TOLERANCE
        and span.ended_seconds <= deadline + TOLERANCE
        and (
            (index == len(spans) - 1 and not pending)
            or close(span.ended_seconds, span.enqueued_seconds + lease)
        )
        and ((index == len(spans) - 1 and not pending) or span.ended_seconds < deadline)
        for index, span in enumerate(spans)
    )
    if receipt.admitted_seconds is not None:
        return valid and receipt.admitted_seconds < min(
            deadline, spans[-1].enqueued_seconds + lease
        )
    if receipt.outcome == "timed_out":
        if pending:
            return valid
        end = min(deadline, spans[-1].enqueued_seconds + lease)
        return (
            valid
            and close(spans[-1].ended_seconds, end)
            and (len(spans) == (step.max_queue_attempts or 1) or close(end, deadline))
        )
    return valid


def reconcile(report: RunReport) -> Code | None:
    """Return the fixed code of the first schedule contradiction; None when sound."""
    schedule = report.schedule
    if not bound(report):
        return "schedule_binding"
    if schedule is None:
        return None
    limits = {
        operation.action: operation.concurrency
        for operation in report.workload.operation_mix
    }
    if schedule.peaks.active > report.workload.concurrent_submissions or any(
        count > limits[action] for action, count in schedule.peaks.per_action
    ):
        return "schedule_concurrency"
    if not timed(report, schedule.receipts):
        return "schedule_times"
    return None
