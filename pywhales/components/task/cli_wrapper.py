"""Swarm task handles and inspection through the configured manager daemon."""

from __future__ import annotations

import builtins
from datetime import datetime, timedelta
from typing import Literal, overload, override

from pydantic import JsonValue

import pywhales.components.service.cli_wrapper
from pywhales._inspect_validation import parse_namespace_inspect_results
from pywhales.client_config import (
    ClientConfig,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components.service._logs import logs_command
from pywhales.components.task.models import (
    AssignedGenericResources,
    ObjectVersion,
    TaskInspectResult,
    TaskSpec,
    TaskStatus,
)
from pywhales.enums import TaskState
from pywhales.utils import (
    ProcessStream,
    run,
    stream_stdout_and_stderr,
    validate_timeout,
)


class Task(ReloadableObjectFromJson[TaskInspectResult]):
    """Reloadable Swarm task handle retaining its originating client settings."""

    def __init__(
        self, client_config: ClientConfig, reference: str, is_immutable_id: bool = False
    ) -> None:
        super().__init__(client_config, "id", reference, is_immutable_id)

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> TaskInspectResult:
        return TaskInspectResult.model_validate(json_object)

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def version(self) -> ObjectVersion | None:
        return self._get_inspect_result().version

    @property
    def created_at(self) -> datetime | None:
        return self._get_inspect_result().created_at

    @property
    def updated_at(self) -> datetime | None:
        return self._get_inspect_result().updated_at

    @property
    def name(self) -> str | None:
        return self._get_inspect_result().name

    @property
    def labels(self) -> dict[str, str] | None:
        return self._get_inspect_result().labels

    @property
    def spec(self) -> TaskSpec | None:
        return self._get_inspect_result().spec

    @property
    def service_id(self) -> str | None:
        return self._get_inspect_result().service_id

    @property
    def slot(self) -> int | None:
        return self._get_inspect_result().slot

    @property
    def node_id(self) -> str | None:
        return self._get_inspect_result().node_id

    @property
    def assigned_generic_resources(self) -> list[AssignedGenericResources] | None:
        return self._get_inspect_result().assigned_generic_resources

    @property
    def status(self) -> TaskStatus | None:
        return self._get_inspect_result().status

    @property
    def desired_state(self) -> TaskState | str | None:
        return self._get_inspect_result().desired_state

    @override
    def __repr__(self) -> str:
        return f"pywhales.Task(id='{self.id[:12]}', name={self.name})"


ValidTask = str | Task


class TaskCLI(DockerCLICaller):
    """Task-inspection commands retaining the originating client configuration."""

    def list(self) -> builtins.list[Task]:
        """List tasks associated with the services listed in the Swarm.

        Returns:
            Task handles from a service-list call followed by service ps.
        """
        service_cli = pywhales.components.service.cli_wrapper.ServiceCLI(
            self.client_config
        )
        return service_cli.ps(service_cli.list())

    @overload
    def inspect(self, x: str) -> Task: ...

    @overload
    def inspect(self, x: builtins.list[str]) -> builtins.list[Task]: ...

    def inspect(self, x: str | builtins.list[str]) -> Task | builtins.list[Task]:
        """Inspect one or more Swarm tasks.

        Args:
            x: One task ID or a list of references. An empty list returns without
                invoking the CLI.

        Returns:
            One handle for a string, otherwise handles in request order. Batch
            inspection preserves duplicates as distinct handles and validates all
            snapshots before hydration. Full IDs must match exactly; aliases use
            positional association.

        Raises:
            ParsingError: If batch shape, task identities, or duplicate snapshot
                consistency is invalid.
        """
        if isinstance(x, str):
            return Task(self.client_config, x)
        else:
            if not x:
                return []
            parser = Task(self.client_config, x[0], is_immutable_id=True)
            snapshots = parse_namespace_inspect_results(
                parser, run([*self.docker_cmd, "inspect", "--type", "task", *x]), x
            )
            tasks = []
            for identity, snapshot in snapshots:
                task = Task(self.client_config, identity, is_immutable_id=True)
                task._set_inspect_result(snapshot)
                tasks.append(task)
            return tasks

    @overload
    def logs(
        self,
        task: ValidTask,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        resolve: bool = True,
        truncate: bool = True,
        stream: Literal[False] = False,
        *,
        task_ids: bool = True,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def logs(
        self,
        task: ValidTask,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        resolve: bool = True,
        truncate: bool = True,
        *,
        stream: Literal[True],
        task_ids: bool = True,
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def logs(
        self,
        task: ValidTask,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        resolve: bool = True,
        truncate: bool = True,
        stream: bool = False,
        *,
        task_ids: bool = True,
        timeout: float | None = None,
    ) -> str | ProcessStream: ...

    def logs(
        self,
        task: ValidTask,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        resolve: bool = True,
        truncate: bool = True,
        stream: bool = False,
        *,
        task_ids: bool = True,
        timeout: float | None = None,
    ) -> str | ProcessStream:
        """Return collected logs of one task or an owned byte stream.

        Docker serves task logs through `docker service logs`, which accepts a
        task ID. Nothing is inspected first: the logs CLI is the only launch, and
        it starts before this call returns, including in streaming mode.
        Collection blocks until the CLI exits and its pipes drain; follow=True can
        wait indefinitely even when stream=False. Closing a stream only cancels
        the local CLI, not the task. Exhaust the stream, use it as a context
        manager, or call close() when stopping early; a bare loop break does not
        release it.

        Args:
            task: Task ID or handle to read logs from.
            details: Include additional log details.
            since: Lower time bound, or None to omit it.
            tail: Number of trailing lines; None leaves Docker's default of all lines.
            timestamps: Include timestamps with log records.
            follow: Keep following new log records instead of taking a snapshot.
            raw: Omit Docker's log formatting.
            resolve: Resolve IDs to names in formatted output.
            truncate: Allow Docker to truncate IDs in formatted output.
            stream: Return a closeable stream when True; otherwise collect decoded
                stdout and stderr records into one string in arrival order.
            task_ids: Include the task ID in formatted output.
            timeout: Positive finite seconds for the local logs CLI, or None to
                inherit the client default. Timing starts at launch and continues
                while iteration pauses. Expiry kills and reaps the local CLI,
                preserving captured output; natural post-exit pipe draining is
                unchanged.

        Returns:
            Collected text when stream=False; otherwise an owned stream of
            (source, content) records, with source equal to stdout or stderr and
            content containing bytes. It inherits the client's stream buffer budget.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerException: If Docker rejects the request, such as an unknown task.
            DockerTimeout: If the logs CLI exceeds its deadline, delivered during
                collection or stream consumption/clean context exit.
            BufferError: If a record or retained stderr history exceeds the
                configured stream buffer budget.
        """
        validate_timeout(timeout)
        full_cmd = logs_command(
            self.docker_cmd,
            task,
            details=details,
            since=since,
            tail=tail,
            timestamps=timestamps,
            follow=follow,
            raw=raw,
            task_ids=task_ids,
            resolve=resolve,
            truncate=truncate,
        )
        iterator = stream_stdout_and_stderr(full_cmd, timeout=timeout)
        if stream:
            return iterator
        with iterator:
            return "".join(record[1].decode() for record in iterator)
