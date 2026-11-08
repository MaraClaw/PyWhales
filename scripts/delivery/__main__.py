"""Delivery CLI: data-only validation before each privileged boundary.

Run with the trusted checkout's locked environment: python -m scripts.delivery.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from . import limits
from .api import Curl, GitHub, Rejected, require
from .artifacts import extract, inventory, select, verified
from .chain import name, project_version, receipt, release_notes, seal
from .deadline import command_deadline
from .distributions import smoke
from .models import Run
from .pages import verify_pages_tar
from .publication import confirm, destination, prepare
from .releases import release, released


def parser() -> argparse.ArgumentParser:
    """Expose only the supported producer/publisher/consumer operations."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "operation",
        choices=(
            "smoke",
            "notes",
            "seal",
            "prepare",
            "confirm",
            "release",
            "pages",
            "verify-pages",
        ),
    )
    result.add_argument("--run-id", type=int)
    result.add_argument("--output", type=Path, default=Path("delivery-output"))
    result.add_argument("--archive", type=Path)
    return result


def main() -> int:
    """Run one operation under the single command deadline in `limits.py`."""
    with command_deadline(limits.COMMAND_SECONDS):
        return execute()


def execute() -> int:
    """Execute a delivery decision, returning nonzero on absent or conflicting proof."""
    arguments = parser().parse_args()
    try:
        if arguments.operation == "smoke":
            smoke(Path.cwd())
            return 0
        if arguments.operation == "notes":
            release_notes(Path.cwd(), project_version(Path.cwd()))
            return 0
        if arguments.operation == "verify-pages":
            require(isinstance(arguments.archive, Path), "Pages archive required")
            verify_pages_tar(arguments.output, arguments.archive)
            return 0
        require(
            arguments.run_id is not None and arguments.run_id > 0,
            "positive run ID required",
        )
        assert isinstance(arguments.run_id, int)
        api = GitHub(
            Curl(),
            os.environ["GITHUB_REPOSITORY"],
            int(os.environ["GITHUB_REPOSITORY_ID"]),
        )
        match arguments.operation:
            case "seal":
                document = seal(api, arguments.run_id, Path.cwd())
                arguments.output.mkdir(parents=True, exist_ok=False)
                (arguments.output / "seal.json").write_text(document.model_dump_json())
            case "prepare":
                prepare(api, arguments.run_id, arguments.output)
                with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                    print(
                        f"pending={'true' if any(arguments.output.iterdir()) else 'false'}",
                        file=output,
                    )
            case "confirm":
                confirmation = confirm(api, arguments.run_id)
                arguments.output.mkdir(parents=True, exist_ok=False)
                (arguments.output / "receipt.json").write_text(
                    confirmation.model_dump_json()
                )
            case "release" | "pages":
                confirmation = receipt(api, arguments.run_id)
                if arguments.operation == "release":
                    require(
                        confirmation.publisher.attempt
                        == int(os.environ["PUBLISHER_ATTEMPT"]),
                        "triggering publisher attempt mismatch",
                    )
                require(
                    not destination(api, confirmation.seal),
                    "destination publication incomplete",
                )
                if arguments.operation == "release":
                    release(api, confirmation)
                else:
                    released(api, confirmation)
                    tested = confirmation.seal
                    run = api.get(
                        f"actions/runs/{tested.producer.run_id}/attempts/{tested.producer.attempt}",
                        Run,
                    )
                    artifact = select(inventory(api, run), name("site", run))
                    extract(verified(api, artifact, tested.site), arguments.output)
        return 0
    except (
        Rejected,
        ValidationError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"delivery rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
