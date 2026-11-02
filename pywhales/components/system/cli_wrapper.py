"""Docker daemon information, rounded disk summaries, and owned event streams."""

import datetime
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Literal

from pywhales._json import loads_compat
from pywhales.client_config import Command, DockerCLICaller
from pywhales.components._json_output import JsonOutputParser
from pywhales.components.system.models import (
    DockerEvent,
    DockerItemsSummary,
    SystemInfo,
)
from pywhales.utils import (
    MappedProcessStream,
    format_filters_for_cli,
    format_time_arg,
    run,
    stream_stdout_and_stderr,
    validate_timeout,
)

type SystemEventsFilter = tuple[str, str]
type SystemPruneFilter = (
    tuple[Literal["until"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["label!"], str]
)


def _parse_event(record: bytes) -> DockerEvent:
    return DockerEvent(**loads_compat(record))


def _parse_info(output: str) -> SystemInfo:
    return SystemInfo(**loads_compat(output))


class DiskFreeResult:
    """Docker-reported disk usage grouped by resource kind.

    Byte totals are derived from the CLI's rounded values, not exact daemon counts.

    Attributes:
        images: Image usage and reclaimable space.
        containers: Container usage and reclaimable space.
        volumes: Local volume usage and reclaimable space.
        build_cache: Build-cache usage and reclaimable space.
    """

    def __init__(self, cli_stdout: str) -> None:
        docker_items = {}
        for line in cli_stdout.splitlines():
            docker_items_dict = loads_compat(line)
            reclamable = docker_items_dict["Reclaimable"]
            docker_items_dict["Reclaimable"] = reclamable.split(" ")[0]
            if "%" in reclamable:
                docker_items_dict["ReclaimablePercent"] = reclamable.split(" ")[1][1:-2]
            else:
                docker_items_dict["ReclaimablePercent"] = "100"

            docker_items[docker_items_dict["Type"]] = docker_items_dict

        self.images: DockerItemsSummary
        self.images = DockerItemsSummary(**docker_items["Images"])
        self.containers: DockerItemsSummary
        self.containers = DockerItemsSummary(**docker_items["Containers"])
        self.volumes: DockerItemsSummary
        self.volumes = DockerItemsSummary(**docker_items["Local Volumes"])
        self.build_cache: DockerItemsSummary
        self.build_cache = DockerItemsSummary(**docker_items["Build Cache"])


class SystemCLI(DockerCLICaller):
    """System commands using the configured CLI, daemon, and execution policy."""

    def disk_free(self) -> DiskFreeResult:
        """Return the selected Docker daemon's disk usage summaries.

        Returns:
            Usage for images, containers, local volumes, and build cache. Byte
            values reflect the CLI's rounding rather than exact daemon totals.

        Raises:
            ParsingError: If the output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """

        full_cmd = Command([*self.docker_cmd, "system", "df", "--format", "{{json .}}"])
        parser = JsonOutputParser(
            "system disk usage", self.client_config.raw_diagnostics
        )
        return parser.parse(DiskFreeResult, run(full_cmd))

    def events(
        self,
        since: datetime.datetime | datetime.timedelta | None = None,
        until: datetime.datetime | datetime.timedelta | None = None,
        filters: Mapping[str, str] | Iterable[SystemEventsFilter] = MappingProxyType(
            {}
        ),
        *,
        timeout: float | None = None,
    ) -> MappedProcessStream[DockerEvent]:
        """Start a closeable stream of events from the selected Docker daemon.

        The local CLI starts during this call, before iteration. Without until,
        it continues waiting for events until closed, failed, or timed out.
        Use the stream as a context manager or call `close()` when stopping
        early. A bare loop break does not release the local CLI process.
        Closing only cancels this local CLI, not daemon-managed resources.

        Args:
            since: Lower event time bound, or None to omit it.
            until: Upper event time bound, or None to keep the subscription open.
            filters: Docker event filter names and values, either as a mapping
                or as an iterable of ``(key, value)`` pairs. Pairs keep their
                order and repeated keys, and an iterator is read once. The
                default adds none. For example, `filters={"type": "container"}`
                or `filters=[("event", "start"), ("event", "die")]`.
            timeout: Positive finite seconds for the local CLI, or None to inherit
                the client default. Timing starts at launch and continues while iteration
                pauses. Expiry kills and reaps the CLI while preserving captured
                output; natural post-exit pipe draining is unchanged.

        Returns:
            An owned iterator mapping stdout JSON records to DockerEvent objects;
            stderr is not yielded as events. It inherits the client's stream
            buffer budget and retains transport ownership through mapping.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up or the CLI starts.
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the CLI exceeds its deadline, delivered during
                iteration or clean context exit.
            BufferError: If a record or retained stderr history exceeds the
                configured stream buffer budget.
            ParsingError: Raised once, during iteration, by the first malformed
                or unexpected stdout record, after the CLI is closed and reaped.
                The client's `raw_diagnostics` flag is read at launch: safe mode
                withholds the payload and chains no cause, raw mode chains the
                original error.
        """
        validate_timeout(timeout)
        filter_args = format_filters_for_cli(filters)
        full_cmd = Command(
            [
                *self.docker_cmd,
                "system",
                "events",
                "--format",
                "{{json .}}",
            ]
        )
        full_cmd.add_simple_arg("--since", format_time_arg(since))
        full_cmd.add_simple_arg("--until", format_time_arg(until))
        full_cmd.add_args_iterable_or_single("--filter", filter_args)
        parser = JsonOutputParser("system event", self.client_config.raw_diagnostics)
        stream = stream_stdout_and_stderr(full_cmd, timeout=timeout)
        return MappedProcessStream(
            stream,
            (
                parser.parse(_parse_event, content)
                for origin, content in stream
                if origin == "stdout"
            ),
        )

    def info(self) -> SystemInfo:
        """Return Docker-reported client and daemon information.

        Returns:
            Parsed system information. Fields omitted by Docker remain None.

        Raises:
            ParsingError: If the output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command(
            [*self.docker_cmd, "system", "info", "--format", "{{json .}}"]
        )
        parser = JsonOutputParser("system info", self.client_config.raw_diagnostics)
        return parser.parse(_parse_info, run(full_cmd))

    def prune(
        self,
        all: bool = False,
        volumes: bool = False,
        filters: Mapping[str, str] | Iterable[SystemPruneFilter] = MappingProxyType({}),
        *,
        timeout: float | None = None,
    ) -> None:
        """Remove unused Docker data without a confirmation prompt.

        Args:
            all: Remove all unused images not just dangling ones
            volumes: Prune volumes
            filters: See the [Docker documentation page about filtering
                ](https://docs.docker.com/reference/cli/docker/system/prune/#filter).
                Pass a mapping, for example `filters=dict(until="24h")`, or an
                iterable of ``(key, value)`` pairs, for example
                `filters=[("label!", "keep"), ("label!", "protect")]`. Pairs
                keep their order and repeated keys, and an iterator is read
                once. The documented keys are `until`, `label`, and `label!`.
            timeout: Positive finite seconds from local CLI launch; `None` inherits
                the client default. Expiry cannot undo daemon-side removal.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
        """
        validate_timeout(timeout)
        filter_args = format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "system", "prune", "--force"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_flag("--volumes", volumes)
        full_cmd.add_args_iterable_or_single("--filter", filter_args)
        run(full_cmd, timeout=timeout)
