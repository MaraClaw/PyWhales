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
        queued[receipt.arrival_seconds] += 1
        if receipt.admitted_seconds is None:
            queued[receipt.terminal_seconds] -= 1
        else:
            queued[receipt.admitted_seconds] -= 1
            active[receipt.admitted_seconds] += 1
            active[receipt.terminal_seconds] -= 1
    return peak(active), peak(queued)


def is_ordered(receipts: Sequence[Receipt]) -> bool:
    """Check numbering, arrival and time order, and that admission is strictly FIFO.

    A launch may neither precede an earlier launch nor happen while an earlier
    submission was still waiting, so it waits for every earlier unlaunched
    terminal (`barrier`).
    """
    arrival = admitted = barrier = 0.0
    for number, receipt in enumerate(receipts, start=1):
        if (
            receipt.submission != number
            or receipt.arrival_seconds < arrival
            or receipt.terminal_seconds < receipt.arrival_seconds
        ):
            return False
        arrival = receipt.arrival_seconds
        launched = receipt.admitted_seconds
        if launched is None:
            if receipt.outcome not in {"timed_out", "cancelled"}:
                return False
            barrier = max(barrier, receipt.terminal_seconds)
        elif max(arrival, admitted, barrier) <= launched <= receipt.terminal_seconds:
            admitted = launched
        else:
            return False
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
    return (
        len(offsets) > 0
        and len(present) == len(offsets)
        and present == sorted(present)
        and report.repetitions == 1
        and len(schedule.receipts) == report.submitted <= len(offsets)
        and all(
            receipt.arrival_seconds == offset
            for receipt, offset in zip(schedule.receipts, offsets, strict=False)
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
    return keys == sorted(set(keys)) and all(
        receipt.arrival_seconds < window
        and receipt.terminal_seconds <= window + TOLERANCE
        and receipt.terminal_seconds
        <= receipt.arrival_seconds + report.operation_timeout_seconds + TOLERANCE
        and consistent(receipt, by_submission[receipt.submission])
        for receipt in receipts
    )


def reconcile(report: RunReport) -> Code | None:
    """Return the fixed code of the first schedule contradiction; None when sound."""
    schedule = report.schedule
    if not bound(report):
        return "schedule_binding"
    if schedule is None:
        return None
    if schedule.peaks.active > report.workload.concurrent_submissions:
        return "schedule_concurrency"
    if not timed(report, schedule.receipts):
        return "schedule_times"
    return None
