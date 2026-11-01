"""Regression boundaries for complete offline evidence, never qualification."""

import json
from pathlib import Path
from typing import Final, Literal, assert_never

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from scripts.fleet_contract.__main__ import safe_errors
from scripts.fleet_contract.report import Evidence, classify
from scripts.fleet_contract.results import RunResult, Sample

FIXTURES: Final = (
    Path(__file__).resolve().parents[2] / "scripts/fleet_contract/fixtures"
)
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


def changed(changes: tuple[tuple[str, JsonValue], ...]) -> str:
    """Edit fixture inputs without bypassing the JSON validation boundary."""
    data: dict[str, JsonValue] = {
        name: JSON_OBJECT.validate_json((FIXTURES / filename).read_text())
        for name, filename in (
            ("manifest", "synthetic-runnable.json"),
            ("support", "support-matrix.json"),
            ("result", "synthetic-result.json"),
        )
    }
    for path, value in changes:
        current: JsonValue = data
        parts = path.split(".")
        for part in parts[:-1]:
            match current:
                case dict():
                    current = current[part]
                case list():
                    current = current[int(part)]
                case _:
                    raise AssertionError(path)
        assert isinstance(current, dict)
        current[parts[-1]] = value
    return json.dumps(data)


@pytest.mark.parametrize("reason", ["excluded", "collection_failed", "unavailable"])
def test_sample_rejected_when_reason_contradicts_measured_seconds(reason: str) -> None:
    raw = changed((("result.latency.0.samples.0.reason", reason),))

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)


@pytest.mark.parametrize(
    ("reason", "seconds", "excluded"),
    [
        ("observed", 0.0, False),
        ("not_launched", 0.0, False),
        ("unavailable", None, False),
        ("collection_failed", None, False),
        ("excluded", None, True),
        ("excluded", 1.0, True),
    ],
)
def test_sample_preserved_when_missing_or_excluded_is_explicit(
    reason: str, seconds: float | None, excluded: bool
) -> None:
    raw = json.dumps(
        {
            "submission": 1,
            "outcome": "failed",
            "reason": reason,
            "seconds": seconds,
            "excluded": excluded,
        }
    )

    sample = Sample.model_validate_json(raw)

    assert sample.seconds == seconds and sample.excluded is excluded
    assert Sample.model_validate_json(sample.model_dump_json()) == sample


def linked(links: tuple[int | None, ...]) -> str:
    """Return the fixture result with its queue-wait sample links replaced."""
    data = JSON_OBJECT.validate_json((FIXTURES / "synthetic-result.json").read_text())
    latency = data["latency"]
    assert isinstance(latency, list) and isinstance(latency[0], dict)
    samples = latency[0]["samples"]
    assert isinstance(samples, list)
    for sample, link in zip(samples, links, strict=True):
        assert isinstance(sample, dict)
        if link is None:
            del sample["submission"]
        else:
            sample["submission"] = link
    return json.dumps(data)


@pytest.mark.parametrize(
    ("links", "path"),
    [
        ((None, 2), "$.latency.0.samples.0.submission"),
        ((0, 2), "$.latency.0.samples.0.submission"),
        ((1, 3), "$.latency"),
        ((1, 1), "$.latency.0"),
    ],
)
def test_unlinked_result_when_sample_links_are_malformed_fails_safely(
    links: tuple[int | None, ...], path: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        RunResult.model_validate_json(linked(links))

    rendered = json.loads(safe_errors(caught.value, RunResult))
    assert [error["path"] for error in rendered["errors"]] == [path]
    assert set(rendered["errors"][0]) == {"code", "path"}
    assert rendered["errors"][0]["code"] in {"missing", "invalid"}


@pytest.mark.parametrize("link", ["1", 1.0, True])
def test_sample_link_when_not_a_strict_integer_is_rejected(link: JsonValue) -> None:
    raw = linked((1, 2)).replace(
        '"submission": 1', f'"submission": {json.dumps(link)}', 1
    )

    with pytest.raises(ValidationError):
        RunResult.model_validate_json(raw)


@pytest.mark.parametrize(
    ("elapsed", "complete"), [(10.0, False), (19.0, False), (20.0, True), (21.0, True)]
)
def test_duration_termination_when_declared_time_is_reached(
    elapsed: float, complete: bool
) -> None:
    evidence = Evidence.model_validate_json(
        changed((("result.elapsed_seconds", elapsed),))
    )

    report = classify(evidence)

    assert report.evidence_complete is complete
    assert report.criteria == ("passed" if complete else "unknown")
    assert report.qualification == "unqualified"


@pytest.mark.parametrize(("target", "complete"), [(1, True), (2, True), (100, False)])
def test_operation_termination_when_submission_target_is_reached(
    target: int, complete: bool
) -> None:
    evidence = Evidence.model_validate_json(
        changed(
            (
                ("manifest.workload.termination", "operation_count"),
                ("manifest.workload.termination_operations", target),
            )
        )
    )

    report = classify(evidence)

    assert report.evidence_complete is complete
    assert report.criteria == ("passed" if complete else "unknown")


@pytest.mark.parametrize("window", [21.0, 100.0])
def test_result_rejected_when_stability_exceeds_elapsed_time(window: float) -> None:
    raw = changed((("result.stability_window_seconds", window),))

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)


def test_asset_ledger_rejected_when_created_entries_exceed_cap() -> None:
    raw = changed((("manifest.ownership.maximum_created_assets", 0),))

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)


def test_asset_ledger_when_created_cap_equals_ledger_excludes_borrowed_assets() -> None:
    raw = changed((("manifest.ownership.maximum_created_assets", 1),))

    report = classify(Evidence.model_validate_json(raw))

    assert report.evidence_complete


@pytest.mark.parametrize("plugin", ["compose", "buildx"])
@pytest.mark.parametrize("status", ["supported", "experimental", "untested"])
def test_applicable_plugin_rejected_when_version_is_null(
    plugin: str, status: str
) -> None:
    raw = changed(
        (
            ("support.rows.2.required_features", [plugin]),
            ("support.rows.2.status", status),
            ("support.rows.2.provenance", "operator_declared"),
            ("support.rows.2.approval_ref", "claimed-review"),
            ("support.rows.2.evidence_refs", ["claimed-evidence"]),
        )
    )

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)


def test_compose_orchestrator_rejected_when_applicable_plugin_is_null() -> None:
    raw = changed((("support.rows.2.orchestrator", "compose"),))

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)


@pytest.mark.parametrize("plugin", ["compose", "buildx"])
def test_observed_plugin_when_required_version_is_missing_is_incomplete(
    plugin: str,
) -> None:
    evidence = Evidence.model_validate_json(
        changed(
            (
                ("manifest.platform.required_features", [plugin]),
                ("support.rows.2.required_features", [plugin]),
                (f"support.rows.2.versions.{plugin}", "0.0.0-synthetic"),
            )
        )
    )

    report = classify(evidence)

    assert not report.evidence_complete and report.criteria == "unknown"
    assert "environment_missing_or_mismatched" in report.flags


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("manifest.authorization.destructive_scenarios", []),
        ("manifest.authorization.allowed_actions", ["inspect", "cleanup"]),
        (
            "manifest.workload.operation_mix",
            [{"action": "inspect", "fraction": 1.0, "concurrency": 1}],
        ),
    ],
)
def test_fault_evidence_when_action_or_scenario_is_absent(
    path: str, value: JsonValue
) -> None:
    raw = changed(((path, value),))

    try:
        evidence = Evidence.model_validate_json(raw)
    except ValidationError:
        return
    report = classify(evidence)

    assert not report.evidence_complete and report.criteria == "unknown"
    assert "fault_declaration_missing" in report.flags


@pytest.mark.parametrize("plugin", ["compose", "buildx"])
@pytest.mark.parametrize("status", ["supported", "experimental", "untested"])
def test_pending_plugin_when_owned_is_only_allowed_in_untested_rows(
    plugin: str, status: Literal["supported", "experimental", "untested"]
) -> None:
    original = JSON_OBJECT.validate_json(changed(()))
    matrix = original["support"]
    assert isinstance(matrix, dict)
    decisions = matrix["decisions"]
    assert isinstance(decisions, list)
    decisions.append(
        {
            "id": "plugin-version",
            "field": f"rows.2.versions.{plugin}",
            "owner": "maintainer",
            "rationale": "Applicable plugin has not been observed",
            "blocks": "qualification",
        }
    )
    raw = changed(
        (
            ("support.rows.2.required_features", [plugin]),
            ("support.rows.2.status", status),
            ("support.rows.2.provenance", "operator_declared"),
            ("support.rows.2.approval_ref", "claimed-review"),
            ("support.rows.2.evidence_refs", ["claimed-evidence"]),
            (f"support.rows.2.versions.{plugin}", {"decision_id": "plugin-version"}),
            ("support.decisions", decisions),
        )
    )

    match status:
        case "untested":
            evidence = Evidence.model_validate_json(raw)
            assert not classify(evidence).evidence_complete
        case "supported" | "experimental":
            with pytest.raises(ValidationError):
                Evidence.model_validate_json(raw)
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("field", ["python", "compose", "buildx"])
def test_observed_versions_rejected_when_they_contain_pending_markers(
    field: str,
) -> None:
    raw = changed(((f"result.observed_versions.{field}", {"decision_id": "unknown"}),))

    with pytest.raises(ValidationError):
        Evidence.model_validate_json(raw)
