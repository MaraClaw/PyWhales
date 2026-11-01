"""Preflight-gated offline execution with bounded load and exact cleanup."""

import hashlib
import json
import math
import platform
from importlib.metadata import version
from typing import Literal

from scripts.fleet_contract.workload import Boundary

from .fake import FakeDriver, Launch
from .models import (
    Plan,
    RunnableManifest,
    authorize,
    deadline_reasons,
    exhausted,
    load_deadline,
    metrics_for,
    resource_reasons,
)
from .records import (
    Cleanup,
    Distribution,
    Environment,
    Load,
    Observation,
    Peaks,
    RunReport,
    Schedule,
    terminal_outcomes,
)
from .scheduler import Simulation
from .slots import AssetLimit, Slots


def summarize(samples: tuple[Observation, ...]) -> tuple[Distribution, ...]:
    """Summarize each boundary including failure timings and unavailable counts."""
    boundaries: tuple[Boundary, ...] = (
        "queue_wait",
        "selected_cli",
        "readiness",
        "recovery",
    )
    summaries: list[Distribution] = []
    for boundary in boundaries:
        matching = [sample for sample in samples if sample.boundary == boundary]
        values = tuple(
            sorted(sample.seconds for sample in matching if sample.seconds is not None)
        )
        summaries.append(
            Distribution(
                boundary=boundary,
                recorded=len(matching),
                unavailable=sum(sample.seconds is None for sample in matching),
                values_seconds=values,
                p50_seconds=values[math.ceil(len(values) * 0.5) - 1]
                if values
                else None,
                p95_seconds=values[math.ceil(len(values) * 0.95) - 1]
                if values
                else None,
                maximum_seconds=values[-1] if values else None,
            )
        )
    return tuple(summaries)


def sequential(manifest: RunnableManifest, plan: Plan, driver: FakeDriver) -> Load:
    """Run each step to completion before the next arrives (no overlap)."""
    budgets, end = manifest.budgets, load_deadline(manifest)
    metrics, slots = metrics_for(plan), Slots(manifest)
    reasons: list[str] = []
    samples: list[Observation] = []
    submitted = failures = unknown = 0
    for index in range(len(plan.steps) * plan.repetitions):
        step = plan.steps[index % len(plan.steps)]
        if driver.now >= end:
            reasons.extend(deadline_reasons(manifest))
            break
        try:
            resource = slots.resolve(step, submitted + 1, driver)
        except AssetLimit:
            reasons.append("asset_limit")
            break
        submitted += 1
        arrival = driver.now
        deadline = min(end, arrival + plan.operation_timeout_seconds)
        queue = driver.advance(step.queue_seconds, deadline)
        observed = driver.execute(
            step, Launch(submitted, deadline, resource, arrival, queue)
        )
        samples.extend(observed)
        outcome = observed[0].outcome
        failures += outcome != "succeeded"
        unknown += outcome == "unknown"
        reasons.extend(resource_reasons(manifest, metrics))
        if unknown and unknown >= budgets.early_stop_unknown_count:
            reasons.append("unknown_limit")
        if driver.now >= end:
            reasons.extend(deadline_reasons(manifest))
            break
        if failures and failures / submitted >= budgets.early_stop_error_fraction:
            reasons.append("failure_limit")
        if reasons:
            break
        if manifest.workload.termination == "operation_count" and submitted >= (
            manifest.workload.termination_operations or 0
        ):
            break
    if not reasons and exhausted(manifest, driver.now, submitted):
        reasons.append("plan_exhausted")
    return Load(tuple(samples), submitted, tuple(reasons))


def run(manifest: RunnableManifest, plan: Plan) -> RunReport:
    """Run only the built-in fake after all declaration gates, then clean ownership.

    Declarations are not security authentication. This function cannot bind or
    contact a real daemon. Fake deadlines prove simulator logic only. Plans with
    arrival offsets overlap through `scheduler.Simulation`; others stay sequential.
    """
    reasons = list(authorize(manifest, plan))
    status: Literal["completed", "stopped", "rejected"] = (
        "rejected" if reasons else "completed"
    )
    driver: FakeDriver | None = None
    load = Load(
        (),
        0,
        (),
        Schedule(peaks=Peaks(active=0, queued=0), receipts=())
        if plan.scheduled
        else None,
    )
    cleanup = Cleanup(
        attempted=(), removed=(), residual=(), elapsed_seconds=0.0, timed_out=False
    )
    if not reasons:
        driver = FakeDriver()
        try:
            load = (
                Simulation(manifest, plan, driver).run()
                if plan.scheduled
                else sequential(manifest, plan, driver)
            )
        finally:
            cleanup = driver.cleanup(plan, manifest.budgets.cleanup_timeout_seconds)
        reasons.extend(load.reasons)
        if cleanup.residual:
            reasons.append("cleanup_residual")
        status = "stopped" if reasons else "completed"
    keys = {"scenario", "endpoint", "action", "asset", "fault"}
    if plan.scheduled:
        keys.add("arrival_seconds")
    operations = [step.model_dump(include=keys) for step in plan.steps]
    operation_digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(operations, sort_keys=True).encode()).hexdigest()
    )
    return RunReport(
        status=status,
        reasons=tuple(sorted(set(reasons))),
        manifest_revision=manifest.identity.revision,
        workload_digest=manifest.identity.workload_digest,
        operation_digest=operation_digest,
        workload=manifest.workload,
        repetitions=plan.repetitions,
        steps=plan.steps,
        operation_timeout_seconds=plan.operation_timeout_seconds,
        environment=Environment(
            python=platform.python_version(),
            library=version("pywhales"),
            platform=manifest.platform,
            topology=manifest.topology,
            images=manifest.workload.images,
        ),
        artifacts=manifest.identity.artifacts,
        provider_calls=driver.calls if driver else 0,
        command_count=driver.commands if driver else 0,
        submitted=load.submitted,
        outcomes=terminal_outcomes(load.samples),
        samples=load.samples,
        distributions=summarize(load.samples),
        metrics=metrics_for(plan),
        ledger=tuple(driver.ledger) if driver else (),
        cleanup=cleanup,
        elapsed_seconds=driver.now if driver else 0.0,
        load_elapsed_seconds=(driver.now - cleanup.elapsed_seconds) if driver else 0.0,
        schedule=load.schedule,
    )
