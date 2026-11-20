#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14.10"
# dependencies = []
# ///
"""Measure simulated wrapper overhead using the checkout's installed dependencies.

Run from the repository root: .venv/bin/python scripts/benchmark_docker_overhead.py
No subprocess is started. This is not a Docker build speed benchmark.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from typing import Final, TextIO, TypedDict, Unpack
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pywhales import DockerClient
from pywhales._json import dumps_compat

IMAGE_ID: Final = "sha256:" + "a" * 64


class Options(BaseModel):
    """Immutable benchmark settings with one to 1000 repetitions per scenario."""

    model_config = ConfigDict(frozen=True, strict=True)
    repetitions: int = Field(default=20, ge=1, le=1000)


class ExecutionOptions(TypedDict, total=False):
    """Subprocess keyword arguments accepted but ignored by simulated execution."""

    input: bytes | None
    stdout: int | TextIO | None
    stderr: int | TextIO | None
    env: Mapping[str, str]
    pass_fds: Sequence[int]


@dataclass(slots=True)
class SimulatedExecution:
    """Mutable request recorder supplying only known benchmark CLI responses.

    Attributes:
        commands: Argument tuples accumulated in request order, including requests
            rejected as unsupported.
    """

    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(
        self, args: Sequence[str], **_options: Unpack[ExecutionOptions]
    ) -> subprocess.CompletedProcess[bytes]:
        """Record a wrapper request and return simulated output without launching.

        Build requests with an image-ID file write the fixture ID to that path.

        Args:
            args: Complete command arguments, including the executable slot.
            **_options: Accepted subprocess settings, unused by the simulator.

        Returns:
            A successful byte-output result for a recognized command.

        Raises:
            RuntimeError: If the requested command has no fixture response.
        """
        command = tuple(args)
        self.commands.append(command)
        if command[1:3] == ("buildx", "bake"):
            output = (
                b'{"target":{"app":{"context":"."}}}' if "--print" in command else b""
            )
        elif command[1:3] == ("buildx", "inspect"):
            output = b"Name: benchmark\nDriver: docker\n"
        elif command[1:3] == ("buildx", "ls"):
            output = b'{"Name":"benchmark","Driver":"docker"}'
        elif command[1:3] == ("buildx", "build"):
            if "--iidfile" in command:
                Path(command[command.index("--iidfile") + 1]).write_text(IMAGE_ID)
            output = b""
        elif command[1:3] == ("image", "inspect"):
            output = dumps_compat([{"Id": IMAGE_ID}]).encode()
        else:
            raise RuntimeError(command)
        return subprocess.CompletedProcess(list(args), 0, stdout=output, stderr=b"")

    def run_isolated(
        self, args: list[str], *_settings: object, **_options: object
    ) -> subprocess.CompletedProcess[bytes]:
        """Simulate a launch that ``run()`` would start in its own process group.

        Without a terminal, ``run()`` starts an owned ``Popen`` instead of calling
        ``subprocess.run``, so the simulator replaces that path too.

        Args:
            args: Complete command arguments, including the executable slot.
            *_settings: Positional launch settings, unused by the simulator.
            **_options: Keyword launch settings, unused by the simulator.

        Returns:
            The same simulated result as ``run``.
        """
        return self.run(args)


@dataclass(frozen=True, slots=True)
class Timing:
    """Per-iteration simulated-wrapper timing summaries in nanoseconds."""

    min_ns: int
    median_ns: float
    max_ns: int


@dataclass(frozen=True, slots=True)
class Measurement:
    """One scenario's aggregate request counts and per-iteration timing summary."""

    name: str
    iterations: int
    execution_requests: int
    builder_inspect_requests: int
    builder_list_requests: int
    simulated_wrapper_timing: Timing


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Versioned report separating simulated wrapper overhead from Docker speed."""

    schema_version: int
    measurement: str
    docker_executed: bool
    python_version: str
    scenarios: tuple[Measurement, ...]


def measure(name: str, operation: Callable[[], None], repetitions: int) -> Measurement:
    """Measure repeated wrapper calls with process execution replaced or denied.

    Temporarily replace subprocess execution and disable debug output globally;
    patches are restored on exit. This measures Python work, not Docker build time.

    Args:
        name: Scenario label stored in the resulting measurement.
        operation: Synchronous wrapper scenario supported by the simulator.
        repetitions: Positive number of calls included in the measurement.

    Returns:
        Aggregate request counts and minimum, median, and maximum call durations.
    """
    execution = SimulatedExecution()
    samples = []
    with (
        patch("subprocess.run", new=execution.run),
        patch("pywhales.utils._run_isolated", new=execution.run_isolated),
        patch("subprocess.Popen", side_effect=RuntimeError("Process execution denied")),
        patch(
            "pywhales.utils.Popen", side_effect=RuntimeError("Process execution denied")
        ),
        patch.dict("os.environ", {"PYWHALES_DEBUG": "0"}),
    ):
        for _ in range(repetitions):
            start = perf_counter_ns()
            operation()
            samples.append(perf_counter_ns() - start)
    return Measurement(
        name=name,
        iterations=repetitions,
        execution_requests=len(execution.commands),
        builder_inspect_requests=sum(
            command[1:3] == ("buildx", "inspect") for command in execution.commands
        ),
        builder_list_requests=sum(
            command[1:3] == ("buildx", "ls") for command in execution.commands
        ),
        simulated_wrapper_timing=Timing(min(samples), median(samples), max(samples)),
    )


def run_benchmark(options: Options) -> BenchmarkReport:
    """Run the five wrapper scenarios without executing Docker or child processes.

    Args:
        options: Validated repetition count applied to every scenario.

    Returns:
        A versioned report containing scenarios in their declared order.
    """
    client = DockerClient(client_call=[sys.executable])

    def bake_default() -> None:
        client.buildx.bake("app")

    def bake_without_config() -> None:
        client.buildx.bake("app", return_config=False)

    def build_image() -> None:
        client.buildx.build(".", tags="benchmark:local")

    def build_push() -> None:
        client.buildx.build(".", tags="benchmark:local", push=True)

    def build_export() -> None:
        client.buildx.build(".", output={"type": "oci", "dest": "unused.tar"})

    operations = (
        ("bake_default", bake_default),
        ("bake_without_config", bake_without_config),
        ("build_image", build_image),
        ("build_push", build_push),
        ("build_export", build_export),
    )
    return BenchmarkReport(
        schema_version=1,
        measurement="simulated_wrapper_overhead_not_docker_speedup",
        docker_executed=False,
        python_version=platform.python_version(),
        scenarios=tuple(
            measure(name, operation, options.repetitions)
            for name, operation in operations
        ),
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Parse benchmark options and print the simulated report as JSON.

    Args:
        argv: Arguments excluding the program name, or None to use sys.argv.

    Raises:
        SystemExit: If argparse handles help, invalid arguments, or an out-of-range
            repetition count.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repetitions",
        type=int,
        default=20,
        metavar="N",
        help="iterations per scenario (1..1000; default: 20)",
    )
    arguments = parser.parse_args(argv)
    try:
        options = Options(repetitions=arguments.repetitions)
    except ValidationError as error:
        parser.error(str(error))
    report = run_benchmark(options)
    print(dumps_compat(asdict(report), indent=2))


if __name__ == "__main__":
    main()
