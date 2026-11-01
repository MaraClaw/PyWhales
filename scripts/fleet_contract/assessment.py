"""Population-aware measurement completeness and preregistered criteria."""

from collections import Counter
from math import ceil

from .common import Amount, Contract, Count, Fraction, Token
from .results import RunResult
from .safety import Budgets
from .workload import Boundary, Measurements


class Percentile(Contract):
    """Nearest-rank percentile over measured, non-excluded outcome samples."""

    fraction: Fraction
    seconds: Amount


class DistributionReport(Contract):
    """Visible measured/excluded/missing denominator and requested percentiles."""

    boundary: Boundary
    expected_count: Count
    recorded_count: Count
    measured_count: Count
    excluded_count: Count
    percentiles: tuple[Percentile, ...]


class Assessment(Contract):
    """Missing measurement flags and observed budget breaches."""

    missing: tuple[Token, ...]
    failed: bool
    distributions: tuple[DistributionReport, ...]


def assess(
    measurements: Measurements, budgets: Budgets, result: RunResult
) -> Assessment:
    """Assess all outcome populations without filling missing evidence with zero.

    Each linked submission must keep one outcome across every boundary, and those
    outcomes must reproduce the aggregate counts.
    """
    missing: set[str] = set()
    failed = False
    distributions: list[DistributionReport] = []
    linked = {
        (sample.submission, sample.outcome)
        for item in result.latency
        for sample in item.samples
    }
    if len(linked) > len({submission for submission, _ in linked}):
        missing.add("submission_outcome_conflict")
    outcomes = result.outcomes
    if linked and Counter(outcome for _, outcome in linked) != Counter(
        succeeded=outcomes.succeeded,
        failed=outcomes.failed,
        rejected=outcomes.rejected,
        cancelled=outcomes.cancelled,
        unknown=outcomes.unknown,
        timed_out=outcomes.timed_out,
    ):
        missing.add("outcome_samples_mismatch")
    for requirement in measurements.latency:
        item = next(
            (item for item in result.latency if item.boundary == requirement.boundary),
            None,
        )
        if item is None:
            missing.add("latency_missing")
            continue
        expected = result.populations.count(requirement.population)
        values = sorted(
            sample.seconds
            for sample in item.samples
            if sample.seconds is not None and not sample.excluded
        )
        excluded = sum(sample.excluded for sample in item.samples)
        if (
            item.population != requirement.population
            or len(item.samples) != expected
            or len(values) != expected
            or not expected
        ):
            missing.add("latency_population_incomplete")
        if excluded:
            missing.add("latency_excluded")
        percentiles = (
            tuple(
                Percentile(
                    fraction=fraction,
                    seconds=values[max(0, ceil(fraction * len(values)) - 1)],
                )
                for fraction in requirement.percentiles
            )
            if values
            else ()
        )
        distributions.append(
            DistributionReport(
                boundary=item.boundary,
                expected_count=expected,
                recorded_count=len(item.samples),
                measured_count=len(values),
                excluded_count=excluded,
                percentiles=percentiles,
            )
        )
        for budget in budgets.latency:
            if budget.boundary == item.boundary:
                failed |= any(
                    point.fraction == budget.percentile
                    and point.seconds > budget.maximum_seconds
                    for point in percentiles
                )
        if item.boundary == "recovery":
            failed |= any(value > budgets.maximum_recovery_seconds for value in values)
    for budget in budgets.resources:
        metric = next(
            (item for item in result.high_water if item.resource == budget.resource),
            None,
        )
        if metric is None or metric.value is None:
            missing.add("high_water_missing")
        else:
            failed |= metric.value > budget.maximum or metric.value > budget.early_stop
    if (
        result.elapsed_seconds is None
        or result.stability_window_seconds is None
        or result.stable_observations is None
    ):
        missing.add("duration_stability_missing")
    else:
        failed |= (
            result.elapsed_seconds < budgets.sustained_duration_seconds
            or result.elapsed_seconds > budgets.early_stop_duration_seconds
            or result.stability_window_seconds < budgets.stability_window_seconds
            or result.stable_observations < budgets.stable_observations
        )
    if result.populations.all_submissions:
        failed |= (
            result.outcomes.accounted - result.outcomes.succeeded
        ) / result.populations.all_submissions > budgets.maximum_error_fraction
    failed |= result.outcomes.unknown >= budgets.early_stop_unknown_count
    return Assessment(
        missing=tuple(sorted(missing)),
        failed=failed,
        distributions=tuple(distributions),
    )
