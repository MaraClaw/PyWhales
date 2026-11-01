"""Swarm task handles and inspection through the configured manager daemon."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import overload, override

from pydantic import JsonValue

import pywhales.components.service.cli_wrapper
from pywhales._inspect_validation import parse_namespace_inspect_results
from pywhales.client_config import (
    ClientConfig,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components.task.models import (
    AssignedGenericResources,
    ObjectVersion,
    TaskInspectResult,
    TaskSpec,
    TaskStatus,
)
from pywhales.enums import TaskState
from pywhales.utils import run


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

    def logs(self) -> None:
        """Reject task log requests because this operation is not implemented.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError
