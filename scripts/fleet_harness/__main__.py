"""Run the fake-only harness or inspect a conditional transport decision."""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from scripts.fleet_contract.__main__ import SafeParser, load, safe_errors
from scripts.fleet_contract.results import RunResult

from .experiment import Experiment, decide
from .models import Plan, RunnableManifest
from .records import ExportRefused, RunReport, export_result
from .runner import run

MODELS: Final[dict[str, type[BaseModel]]] = {
    "manifest": RunnableManifest,
    "plan": Plan,
    "experiment": Experiment,
}


def main(arguments: Sequence[str] | None = None) -> int:
    """Emit simulation JSON; reject invalid input before provider calls.

    Exit 0 means a report was produced, 2 means invalid or rejected input.
    A stopped workload or no-go decision remains visible in the JSON artifact.
    """
    parser = SafeParser(
        description="Offline fleet simulation only; real execution is blocked."
    )
    parser.add_argument("command", choices=("run", "decide", "export", "schema"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--kind", choices=tuple(MODELS), default="plan")
    options = parser.parse_args(arguments)
    if options.command == "schema":
        print(
            json.dumps(
                MODELS[options.kind].model_json_schema(), sort_keys=True, indent=2
            )
        )
        return 0
    if options.input is None:
        parser.error("arguments")
    if options.command == "export":
        if options.manifest is None:
            parser.error("arguments")
        manifest = load(RunnableManifest, options.manifest)
        report = load(RunReport, options.input)
        if manifest is None or report is None:
            return 2
        try:
            result = export_result(report, manifest)
        except ExportRefused:
            print('{"errors":[{"code":"rejected","path":"$"}]}', file=sys.stderr)
            return 2
        except ValidationError as error:
            print(safe_errors(error, RunResult), file=sys.stderr)
            return 2
        print(result.model_dump_json(indent=2))
        return 0
    if options.command == "decide":
        experiment = load(Experiment, options.input)
        if experiment is None:
            print(
                '{"provider_calls":0,"connections":0,"candidate_launches":0,"status":"rejected","simulation":true}'
            )
            return 2
        print(decide(experiment).model_dump_json(indent=2))
        return 0
    if options.manifest is None:
        parser.error("arguments")
    manifest = load(RunnableManifest, options.manifest)
    plan = load(Plan, options.input)
    if manifest is None or plan is None:
        print(
            '{"provider_calls":0,"connections":0,"status":"rejected","simulation":true}'
        )
        return 2
    try:
        report = run(manifest, plan)
    except ValidationError as error:
        print(safe_errors(error, RunnableManifest), file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2))
    return 2 if report.status == "rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
