"""Offline script tooling that keeps stdlib JSON bytes and stays SDK independent.

The fleet harness digest and the benchmark report belong to contributor tooling,
not to installed APIs. Both are pinned byte for byte so a serializer change shows
up here, and the harness runs in a child process to prove it loads no SDK module.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from scripts.benchmark_docker_overhead import main as benchmark
from scripts.fleet_harness.models import Plan, RunnableManifest
from scripts.fleet_harness.runner import run
from tests.pywhales.test_fleet_harness import at, overlap_manifest, overlap_plan

ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = ROOT / "scripts/fleet_harness/fixtures"
OPERATION_KEYS: Final = ("scenario", "endpoint", "action", "asset", "fault")
OPERATION_DIGEST: Final = (
    "sha256:f3e241c7fc5af586e20ffd9295ddbdbb2b0ce25c3e4550336643ec6b05fe3e6f"
)
CANONICAL_OPERATIONS: Final = (
    '[{"action": "create", "asset": "example-container", "endpoint": "example-local", '
    '"fault": null, "scenario": "lifecycle"}, '
    '{"action": "inspect", "asset": null, "endpoint": "example-context", '
    '"fault": null, "scenario": "inventory"}, '
    '{"action": "fault", "asset": null, "endpoint": "example-local", '
    '"fault": "synthetic-fault-not-authority", "scenario": "recovery"}]'
)
CHILD: Final = """
import contextlib, io, json, sys
from scripts.fleet_harness.__main__ import main

output = io.StringIO()
with contextlib.redirect_stdout(output):
    code = main(["run", "--manifest", sys.argv[1], "--input", sys.argv[2]])
loaded = sorted(
    name for name in sys.modules if name == "pywhales" or name.startswith("pywhales.")
)
digest = json.loads(output.getvalue())["operation_digest"]
print(json.dumps({"code": code, "digest": digest, "sdk": loaded}))
"""


def test_run_digest_hashes_sorted_operations_with_default_separators() -> None:
    raw = json.loads((FIXTURES / "plan.json").read_text())
    operations = [
        {key: step.get(key) for key in OPERATION_KEYS} for step in raw["steps"]
    ]
    manifest = RunnableManifest.model_validate_json(
        (FIXTURES / "manifest.json").read_text()
    )
    plan = Plan.model_validate_json((FIXTURES / "plan.json").read_text())

    report = run(manifest, plan)

    assert json.dumps(operations, sort_keys=True) == CANONICAL_OPERATIONS
    assert report.operation_digest == OPERATION_DIGEST
    spaced = hashlib.sha256(CANONICAL_OPERATIONS.encode()).hexdigest()
    assert report.operation_digest == f"sha256:{spaced}"
    compact = json.dumps(operations, sort_keys=True, separators=(",", ":"))
    assert (
        report.operation_digest
        != f"sha256:{hashlib.sha256(compact.encode()).hexdigest()}"
    )


def test_fleet_harness_run_loads_no_sdk_module_and_keeps_digest(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(FIXTURES / "manifest.json"),
            str(FIXTURES / "plan.json"),
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "code": 0,
        "digest": OPERATION_DIGEST,
        "sdk": [],
    }


def test_scheduled_run_digest_binds_offsets_and_keeps_sequential_digest() -> None:
    # Given overlapping plans that differ only in one arrival offset.
    declaration = overlap_manifest(2)
    recipe = overlap_plan(at(0.0), at(0.5))
    moved = overlap_plan(at(0.0), at(0.6))
    operations = [
        {key: step.get(key) for key in (*OPERATION_KEYS, "arrival_seconds")}
        for step in json.loads(recipe.model_dump_json())["steps"]
    ]

    # When each is run, and the sequential fixture is run again.
    scheduled = run(declaration, recipe).operation_digest
    shifted = run(declaration, moved).operation_digest
    sequential = run(
        RunnableManifest.model_validate_json((FIXTURES / "manifest.json").read_text()),
        Plan.model_validate_json((FIXTURES / "plan.json").read_text()),
    ).operation_digest

    # Then the offsets sit in the same stdlib-hashed form, and sequential is unchanged.
    spaced = hashlib.sha256(json.dumps(operations, sort_keys=True).encode()).hexdigest()
    assert scheduled == f"sha256:{spaced}"
    assert shifted != scheduled
    assert sequential == OPERATION_DIGEST


def test_scheduled_fleet_harness_run_loads_no_sdk_module(tmp_path: Path) -> None:
    # Given fictional overlapping inputs on disk and a PATH without Docker.
    declaration, recipe = tmp_path / "manifest.json", tmp_path / "plan.json"
    declaration.write_text(overlap_manifest(2).model_dump_json())
    steps = (at(0.0), at(0.5, 2.0), at(0.5))
    recipe.write_text(overlap_plan(*steps).model_dump_json())

    # When the harness CLI runs them in a fresh interpreter.
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, str(declaration), str(recipe)],
        cwd=ROOT,
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    # Then it succeeds with the in-process digest and imported no SDK module.
    assert json.loads(completed.stdout) == {
        "code": 0,
        "digest": run(overlap_manifest(2), overlap_plan(*steps)).operation_digest,
        "sdk": [],
    }


def test_benchmark_report_is_stdlib_indent_two_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    benchmark(["--repetitions", "1"])

    text = capsys.readouterr().out
    assert text.startswith(
        '{\n  "schema_version": 1,\n'
        '  "measurement": "simulated_wrapper_overhead_not_docker_speedup",\n'
        '  "docker_executed": false,\n  "python_version": "'
    )
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
