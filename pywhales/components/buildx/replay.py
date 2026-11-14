"""Docker Buildx build replay from recorded provenance (experimental).

The command launches the configured Docker CLI and its Buildx plugin. Buildx
marks `replay` experimental, so its options can change between releases.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, overload

from pydantic import JsonValue, TypeAdapter

import pywhales.components.buildx.cli_wrapper
from pywhales.client_config import Command, DockerCLICaller
from pywhales.components._json_output import JsonOutputParser
from pywhales.utils import MappedProcessStream, run, to_list, validate_timeout

type ReplayOutput = str | Mapping[str, str | Path]
type ReplayProgress = Literal["auto", "plain", "tty", "quiet", "rawjson"]
type ReplayPlan = dict[str, JsonValue]

_PLAN_ADAPTER: Final = TypeAdapter(ReplayPlan)


def _output_specs(outputs: ReplayOutput | Sequence[ReplayOutput]) -> list[str]:
    items = [outputs] if isinstance(outputs, str | Mapping) else outputs
    return [
        item
        if isinstance(item, str)
        else pywhales.components.buildx.cli_wrapper.format_dict_for_buildx(item)
        for item in items
    ]


class BuildxReplayCLI(DockerCLICaller):
    """Experimental `docker buildx replay` command group."""

    @overload
    def build(
        self,
        subject: str,
        *,
        load: bool = False,
        network: Literal["default", "none"] | None = None,
        outputs: ReplayOutput | Sequence[ReplayOutput] = (),
        platforms: str | Sequence[str] = (),
        progress: ReplayProgress | None = None,
        push: bool = False,
        replay_mode: Literal["materials", "frontend"] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | Sequence[str] = (),
        tags: str | Sequence[str] = (),
        dry_run: Literal[True],
        stream_logs: Literal[False] = False,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> ReplayPlan: ...

    @overload
    def build(
        self,
        subject: str,
        *,
        load: bool = False,
        network: Literal["default", "none"] | None = None,
        outputs: ReplayOutput | Sequence[ReplayOutput] = (),
        platforms: str | Sequence[str] = (),
        progress: ReplayProgress | None = None,
        push: bool = False,
        replay_mode: Literal["materials", "frontend"] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | Sequence[str] = (),
        tags: str | Sequence[str] = (),
        dry_run: Literal[False] = False,
        stream_logs: Literal[True],
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    @overload
    def build(
        self,
        subject: str,
        *,
        load: bool = False,
        network: Literal["default", "none"] | None = None,
        outputs: ReplayOutput | Sequence[ReplayOutput] = (),
        platforms: str | Sequence[str] = (),
        progress: ReplayProgress | None = None,
        push: bool = False,
        replay_mode: Literal["materials", "frontend"] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | Sequence[str] = (),
        tags: str | Sequence[str] = (),
        dry_run: Literal[False] = False,
        stream_logs: Literal[False] = False,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def build(
        self,
        subject: str,
        *,
        load: bool = False,
        network: Literal["default", "none"] | None = None,
        outputs: ReplayOutput | Sequence[ReplayOutput] = (),
        platforms: str | Sequence[str] = (),
        progress: ReplayProgress | None = None,
        push: bool = False,
        replay_mode: Literal["materials", "frontend"] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | Sequence[str] = (),
        tags: str | Sequence[str] = (),
        dry_run: bool = False,
        stream_logs: bool = False,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> ReplayPlan | MappedProcessStream[str] | None: ...

    def build(
        self,
        subject: str,
        *,
        load: bool = False,
        network: Literal["default", "none"] | None = None,
        outputs: ReplayOutput | Sequence[ReplayOutput] = (),
        platforms: str | Sequence[str] = (),
        progress: ReplayProgress | None = None,
        push: bool = False,
        replay_mode: Literal["materials", "frontend"] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | Sequence[str] = (),
        tags: str | Sequence[str] = (),
        dry_run: bool = False,
        stream_logs: bool = False,
        builder: str | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> ReplayPlan | MappedProcessStream[str] | None:
        """Rebuild an image from its provenance and pinned materials.

        A normal call waits for the local CLI to exit and returns `None`; the
        rebuilt image goes where `load`, `push` or `outputs` send it. Streaming
        starts the local CLI before returning. Closing a stream reaps only the
        local CLI and does not guarantee that the daemon stops the replay.

        Args:
            subject: Image reference to replay.
            load: Load the result into the Docker image store.
            network: Network mode for `RUN` instructions; `None` keeps the mode
                of the original build.
            outputs: Output destination or destinations, passed as repeated
                `--output` flags. A string is forwarded as is; a mapping becomes
                comma-joined `key=value` pairs, such as
                `{"type": "local", "dest": "out"}`.
            platforms: Platform or platforms of the subject to replay, passed as
                repeated `--platform` flags.
            progress: Progress rendering mode; `None` leaves the Buildx default,
                `auto`.
            push: Push the result to a registry.
            replay_mode: Replay `materials` or `frontend`; `None` leaves the
                Buildx default, `materials`.
            secrets: Secret specification or specifications, such as
                `id=mysecret,src=/local/secret`; expose only trusted local files.
            ssh: SSH agent socket or keys to expose to a trusted replay.
            tags: Tag or tags for the result.
            dry_run: Print the replay plan without solving or exporting. The call
                adds `--format json` and returns the parsed plan.
            stream_logs: Return an owned stream of decoded output chunks instead
                of waiting for the CLI to finish; cannot be combined with
                `dry_run`.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`. Its log is part of a stream, but a blocking or
                dry-run call does not return stderr.
            timeout: Positive finite seconds for the local CLI, replacing the
                client default; `None` inherits it. The local CLI is killed and
                reaped on expiry; stream deadlines keep running while the stream
                is not consumed.

        Returns:
            The parsed plan when `dry_run=True`; an owned stream of text chunks
            when `stream_logs=True`; otherwise `None` after the CLI completes.

        Raises:
            ValueError: `timeout` is not positive and finite, or `dry_run` is
                combined with `stream_logs`. Raised before the CLI is looked up.
            ParsingError: The dry-run output is malformed or is not a JSON
                object. Safe mode withholds the payload and chains no cause; a
                client with `raw_diagnostics=True` chains the original error.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The Buildx command fails, for example because the
                subject has no usable provenance or the daemon lacks support.
        """
        validate_timeout(timeout)
        if dry_run and stream_logs:
            raise ValueError("dry_run cannot be combined with stream_logs")
        full_cmd = Command([*self.docker_cmd, "buildx", "replay", "build"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        if dry_run:
            full_cmd.extend(["--dry-run", "--format", "json"])
        full_cmd.add_flag("--load", load)
        full_cmd.add_simple_arg("--network", network)
        full_cmd.add_args_iterable("--output", _output_specs(outputs))
        full_cmd.add_args_iterable_or_single("--platform", to_list(platforms))
        full_cmd.add_simple_arg("--progress", progress)
        full_cmd.add_flag("--push", push)
        full_cmd.add_simple_arg("--replay-mode", replay_mode)
        full_cmd.add_args_iterable_or_single("--secret", to_list(secrets))
        full_cmd.add_args_iterable_or_single("--ssh", to_list(ssh))
        full_cmd.add_args_iterable_or_single("--tag", to_list(tags))
        full_cmd.append(subject)
        if stream_logs:
            return pywhales.components.buildx.cli_wrapper.stream_buildx_logs(
                full_cmd, timeout=timeout
            )
        output = run(full_cmd, timeout=timeout)
        if dry_run:
            parser = JsonOutputParser("replay plan", self.client_config.raw_diagnostics)
            return parser.parse(_PLAN_ADAPTER.validate_json, output)
        return None
