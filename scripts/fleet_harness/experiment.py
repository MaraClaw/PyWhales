"""Conditional transport decisions over prerecorded, non-authorizing evidence."""

from typing import Literal

from scripts.fleet_contract.common import Amount, Contract, Digest, Positive, Token

from .records import RunReport


class Baseline(Contract):
    """External entry declarations; references cannot authenticate real evidence."""

    provenance: Literal["simulation", "real"]
    representative: bool
    independent_review_ref: Token | None
    operator_objective_ref: Token | None
    workload_digest: Digest
    environment_digest: Digest
    transport_seconds: tuple[Amount, ...]
    material_overhead_seconds: Positive


class Semantics(Contract):
    """Explicit semantic contracts for a narrowly selected operation set."""

    authentication: Token
    endpoint_verification: Token
    api_versions: tuple[Token, ...]
    errors: Token
    permissions: Token
    result_shape: Token
    stream_ordering: Token
    backpressure: Token
    byte_accounting: Token
    ownership: Token
    early_close: Token
    cancellation: Token
    timeout: Token
    ambiguous_mutation: Token
    confidentiality: Token


class Comparison(Contract):
    """Two prerecorded simulations and explicit correctness contracts."""

    cli: RunReport
    candidate: RunReport
    cli_contract: Semantics
    candidate_contract: Semantics


class Experiment(Contract):
    """Optional entry evidence and offline comparisons; never launches a backend."""

    baseline: Baseline | None = None
    comparison: Comparison | None = None


class Decision(Contract):
    """No-go record preserving the CLI regardless of simulated equivalence."""

    schema_version: Literal[1] = 1
    simulation: Literal[True] = True
    decision: Literal["no_go"] = "no_go"
    selected_route: Literal["cli"] = "cli"
    compose_buildx_route: Literal["cli_unchanged"] = "cli_unchanged"
    candidate_launches: Literal[0] = 0
    connections: Literal[0] = 0
    qualification: Literal["unqualified"] = "unqualified"
    reasons: tuple[str, ...]
    comparison_status: Literal["not_requested", "matched_simulation", "rejected"]
    mismatches: tuple[str, ...]
    evidence: Experiment


def decide(experiment: Experiment) -> Decision:
    """Reject absent real material evidence and semantic regressions without I/O.

    Even declared real evidence cannot enable execution until a binding and
    deadline adapter has independently qualified. Comparisons are offline only.
    """
    reasons = ["real_binding_and_deadline_adapter_unqualified"]
    baseline = experiment.baseline
    if baseline is None:
        reasons.append("real_material_overhead_baseline_missing")
    elif (
        baseline.provenance != "real"
        or not baseline.representative
        or baseline.independent_review_ref is None
        or baseline.operator_objective_ref is None
    ):
        reasons.append("real_material_overhead_baseline_unqualified")
    elif (
        not baseline.transport_seconds
        or sum(baseline.transport_seconds) / len(baseline.transport_seconds)
        < baseline.material_overhead_seconds
    ):
        reasons.append("material_overhead_entry_unmet")
    mismatches: list[str] = []
    status: Literal["not_requested", "matched_simulation", "rejected"] = "not_requested"
    comparison = experiment.comparison
    if comparison is not None:
        cli_binding = comparison.cli.binding
        candidate_binding = comparison.candidate.binding
        if cli_binding is None or candidate_binding is None:
            mismatches.append("scope.binding_missing")
        else:
            if cli_binding.manifest_digest != candidate_binding.manifest_digest:
                mismatches.append("scope.manifest_digest")
            if cli_binding.environment_digest != candidate_binding.environment_digest:
                mismatches.append("scope.environment_digest")
        for name in Semantics.model_fields:
            if getattr(comparison.cli_contract, name) != getattr(
                comparison.candidate_contract, name
            ):
                mismatches.append(f"contract.{name}")
        if comparison.cli_contract.ambiguous_mutation != "unknown_no_retry_no_rollback":
            mismatches.append("cli.ambiguous_mutation")
        if comparison.cli_contract.confidentiality != "no_credentials_argv_or_payloads":
            mismatches.append("cli.confidentiality")
        for name in (
            "workload_digest",
            "operation_digest",
            "workload",
            "repetitions",
            "environment",
            "operation_timeout_seconds",
            "outcomes",
        ):
            if getattr(comparison.cli, name) != getattr(comparison.candidate, name):
                mismatches.append(f"scope.{name}")
        cli_outcomes = [
            (
                sample.submission,
                sample.scenario,
                sample.boundary,
                sample.outcome,
                sample.seconds is None,
            )
            for sample in comparison.cli.samples
        ]
        candidate_outcomes = [
            (
                sample.submission,
                sample.scenario,
                sample.boundary,
                sample.outcome,
                sample.seconds is None,
            )
            for sample in comparison.candidate.samples
        ]
        if cli_outcomes != candidate_outcomes:
            mismatches.append("observed_outcomes")
        if (
            comparison.cli.status != "completed"
            or comparison.candidate.status != "completed"
        ):
            mismatches.append("incomplete_run")
        if comparison.cli.cleanup.residual or comparison.candidate.cleanup.residual:
            mismatches.append("cleanup_residual")
        if (
            baseline is not None
            and baseline.workload_digest != comparison.cli.workload_digest
        ):
            mismatches.append("baseline.workload_digest")
        if baseline is not None and (
            cli_binding is None
            or baseline.environment_digest != cli_binding.environment_digest
        ):
            mismatches.append("baseline.environment_digest")
        status = "rejected" if mismatches else "matched_simulation"
        reasons.append(
            "comparison_semantic_mismatch"
            if mismatches
            else "simulation_cannot_justify_adoption"
        )
    return Decision(
        reasons=tuple(reasons),
        comparison_status=status,
        mismatches=tuple(mismatches),
        evidence=experiment,
    )
