"""Conversion of a fake run report into synthetic, non-authorizing contract evidence."""

from scripts.fleet_contract.results import (
    HighWater,
    LatencyEvidence,
    Population,
    Populations,
    RunResult,
    Sample,
)
from scripts.fleet_contract.workload import Boundary, Resource

from .models import RunnableManifest
from .records import RunReport, fingerprint


class ExportRefused(Exception):
    """Fixed, input-independent refusal to export a simulation."""


def export_result(report: RunReport, manifest: RunnableManifest) -> RunResult:
    """Convert a fake run to synthetic evidence without inventing receipts."""
    if (
        manifest.provenance != "synthetic"
        or report.status == "rejected"
        or report.binding is None
        or report.binding.manifest_digest != fingerprint(manifest)
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
