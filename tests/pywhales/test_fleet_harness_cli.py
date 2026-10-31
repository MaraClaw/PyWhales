"""Actual offline CLI reports, failure receipts and safe input errors."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.fleet_harness.__main__ import main
from scripts.fleet_harness.experiment import Comparison, Experiment
from scripts.fleet_harness.runner import run
from tests.pywhales.test_fleet_harness import (
    FIXTURES,
    duration_manifest,
    duration_plan,
    manifest,
    plan,
)
from tests.pywhales.test_transport_experiment import semantics


def invoke(arguments: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.fleet_harness", *arguments],
        env={**os.environ, "PATH": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_export_cli_when_simulation_is_reported_without_docker(tmp_path: Path) -> None:
    simulation = tmp_path / "simulation.json"
    simulation.write_text(run(manifest(), plan()).model_dump_json())
    exported = invoke(
        [
            "export",
            "--manifest",
            str(FIXTURES / "manifest.json"),
            "--input",
            str(simulation),
        ],
        tmp_path,
    )
    assert exported.returncode == 0
    result = json.loads(exported.stdout)
    assert result["provenance"] == "synthetic"
    assert result["authorization_record"] is None
    assert all(
        result[name] is None
        for name in (
            "isolation_evidence_ref",
            "artifact_receipt_ref",
            "cleanup_receipt_ref",
        )
    )
    assert [s["submission"] for s in result["latency"][0]["samples"]] == [1, 2, 3]
    result_path = tmp_path / "result.json"
    result_path.write_text(exported.stdout)
    contract = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.fleet_contract",
            "report",
            "--manifest",
            str(FIXTURES / "manifest.json"),
            "--support",
            str(FIXTURES.parent.parent / "fleet_contract/fixtures/support-matrix.json"),
            "--input",
            str(result_path),
        ],
        env={**os.environ, "PATH": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert contract.returncode == 0, contract.stderr
    assessed = json.loads(contract.stdout)
    assert (
        not assessed["evidence_complete"] and assessed["qualification"] == "unqualified"
    )
    assert {"receipts_missing", "authorization_record_mismatch"} <= set(
        assessed["flags"]
    )


@pytest.mark.parametrize("defect", ["rejected", "non_synthetic", "invalid"])
def test_export_cli_when_input_is_refused_is_safe(defect: str, tmp_path: Path) -> None:
    declaration = json.loads(manifest().model_dump_json())
    simulation = json.loads(run(manifest(), plan()).model_dump_json())
    if defect == "rejected":
        simulation.update(status="rejected", reasons=["refused"])
    elif defect == "non_synthetic":
        declaration["provenance"] = "operator_declared"
        declaration["authorization"]["purpose"] = "operator_declared"
    else:
        simulation["private-secret"] = "private-secret"
    manifest_path, report_path = tmp_path / "manifest.json", tmp_path / "report.json"
    manifest_path.write_text(json.dumps(declaration))
    report_path.write_text(json.dumps(simulation))
    output = invoke(
        ["export", "--manifest", str(manifest_path), "--input", str(report_path)],
        tmp_path,
    )
    assert output.returncode == 2
    assert json.loads(output.stderr)["errors"]
    assert "private-secret" not in output.stderr


def test_run_cli_when_docker_is_absent_writes_simulated_cleanup_report(
    tmp_path: Path,
) -> None:
    result = invoke(
        [
            "run",
            "--manifest",
            str(FIXTURES / "manifest.json"),
            "--input",
            str(FIXTURES / "plan.json"),
        ],
        tmp_path,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["simulation"] is True
    assert report["status"] == "completed" and report["outcomes"]["succeeded"] == 3
    assert report["cleanup"]["removed"] == report["ledger"]
    assert (
        report["provider_calls"]
        == 4
        == report["command_count"] + len(report["cleanup"]["attempted"])
    )
    assert report["environment"]["docker_engine"] is None
    assert "private-example-connection" not in result.stdout


@pytest.mark.parametrize("field,value", [("opt_in", False), ("provider", "real")])
def test_run_cli_when_unapproved_returns_zero_call_receipt(
    field: str, value: str | bool, tmp_path: Path
) -> None:
    raw = json.loads((FIXTURES / "plan.json").read_text())
    raw[field] = value
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw))

    result = invoke(
        ["run", "--manifest", str(FIXTURES / "manifest.json"), "--input", str(path)],
        tmp_path,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 2 and report["status"] == "rejected"
    assert (
        report["provider_calls"]
        == report["connections"]
        == report["command_count"]
        == 0
    )


@pytest.mark.parametrize(
    "field",
    [
        "ownership_marker",
        "isolation_evidence_ref",
        "effective_endpoint_allowlist",
        "maximum_run_seconds",
        "cleanup_surface_ref",
    ],
)
def test_run_cli_when_authorization_fields_are_missing_never_starts(
    field: str, tmp_path: Path
) -> None:
    raw = json.loads((FIXTURES / "manifest.json").read_text())
    del raw["authorization"][field]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw))

    result = invoke(
        ["run", "--manifest", str(path), "--input", str(FIXTURES / "plan.json")],
        tmp_path,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 2 and report["provider_calls"] == 0
    assert json.loads(result.stderr)["errors"]


def test_decision_cli_when_baseline_is_absent_emits_no_go(tmp_path: Path) -> None:
    result = invoke(["decide", "--input", str(FIXTURES / "no-baseline.json")], tmp_path)

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["decision"] == "no_go" and report["candidate_launches"] == 0
    assert report["selected_route"] == "cli" and report["simulation"] is True


@pytest.mark.parametrize(
    "raw",
    [
        '{"credential-secret":"payload-secret"}',
        "secret-invalid-json",
        '{"operation_timeout_seconds":NaN}',
    ],
)
def test_cli_errors_when_input_contains_private_values_are_fixed_codes(
    raw: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "secret-path.json"
    path.write_text(raw)

    status = main(
        ["run", "--manifest", str(FIXTURES / "manifest.json"), "--input", str(path)]
    )

    output = capsys.readouterr()
    assert status == 2 and json.loads(output.out)["provider_calls"] == 0
    assert "secret" not in output.err
    assert {item["code"] for item in json.loads(output.err)["errors"]} <= {
        "invalid",
        "missing",
    }


def test_decision_cli_when_aggregate_outcomes_disagree_rejects_input(
    tmp_path: Path,
) -> None:
    result = run(manifest(), plan())
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    raw = json.loads(Experiment(comparison=comparison).model_dump_json())
    raw["comparison"]["candidate"]["outcomes"].update(succeeded=2, failed=1)
    path = tmp_path / "inconsistent.json"
    path.write_text(json.dumps(raw))

    output = invoke(["decide", "--input", str(path)], tmp_path)

    assert output.returncode == 2
    assert json.loads(output.stdout)["candidate_launches"] == 0


def test_decision_cli_when_private_connection_reference_is_imported_withholds_it(
    tmp_path: Path,
) -> None:
    result = run(manifest(), plan())
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    raw = json.loads(Experiment(comparison=comparison).model_dump_json())
    raw["comparison"]["candidate"]["environment"]["platform"]["endpoints"][0][
        "connection_ref"
    ] = "private-connection-secret"
    path = tmp_path / "private.json"
    path.write_text(json.dumps(raw))

    output = invoke(["decide", "--input", str(path)], tmp_path)

    assert output.returncode == 0
    assert "private-connection-secret" not in output.stdout + output.stderr
    evidence = json.loads(output.stdout)["evidence"]["comparison"]["candidate"]
    assert (
        evidence["environment"]["platform"]["endpoints"][0]["connection_ref"]
        == "withheld"
    )


def test_run_cli_when_duration_finishes_reports_completed_without_docker(
    tmp_path: Path,
) -> None:
    # Given validated duration input and a clipped final submission.
    declaration = tmp_path / "duration.json"
    declaration.write_text(duration_manifest().model_dump_json())
    recipe = tmp_path / "plan.json"
    recipe.write_text(duration_plan(12.0, 2).model_dump_json())

    # When the actual CLI executes with Docker absent from PATH.
    output = invoke(
        ["run", "--manifest", str(declaration), "--input", str(recipe)], tmp_path
    )

    # Then the JSON artifact reports a full duration without hiding its timeout.
    report = json.loads(output.stdout)
    assert output.returncode == 0 and report["status"] == "completed"
    assert report["reasons"] == [] and report["load_elapsed_seconds"] == 20.0
    assert report["outcomes"]["timed_out"] == 1
    assert report["provider_calls"] == report["command_count"] == 2


@pytest.mark.parametrize("route", ["cli", "candidate"])
@pytest.mark.parametrize("defect", ["provider_calls", "status"])
def test_decision_cli_when_imported_report_is_incoherent_rejects_without_launches(
    route: str, defect: str, tmp_path: Path
) -> None:
    # Given an imported comparison with incorrect accounting or status reasons.
    result = run(manifest(), plan())
    comparison = Comparison(
        cli=result,
        candidate=result,
        cli_contract=semantics(),
        candidate_contract=semantics(),
    )
    raw = json.loads(Experiment(comparison=comparison).model_dump_json())
    for report in (raw["comparison"]["cli"], raw["comparison"]["candidate"]):
        report["provider_calls"] = 4
    raw["comparison"][route][defect] = 5 if defect == "provider_calls" else "stopped"
    path = tmp_path / "incoherent.json"
    path.write_text(json.dumps(raw))

    # When the actual decide CLI loads the nested report.
    output = invoke(["decide", "--input", str(path)], tmp_path)

    # Then validation rejects before any candidate launch and emits safe errors.
    assert output.returncode == 2
    assert json.loads(output.stdout)["candidate_launches"] == 0
    assert {error["code"] for error in json.loads(output.stderr)["errors"]} == {
        "invalid"
    }
