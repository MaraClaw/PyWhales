"""Swarm config handles and CLI operations requiring a manager daemon."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Literal, Self, assert_never, overload, override

from pydantic import JsonValue

from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components._hydration import hydrate_handles, release_pins
from pywhales.components._swarm_inspect import parse_swarm_inspect_results
from pywhales.components.config.models import (
    ConfigInspectResult,
    ConfigSpec,
    DockerObjectVersion,
)
from pywhales.exceptions import NoSuchConfig
from pywhales.utils import (
    format_filters_for_cli,
    format_mapping_for_cli,
    run,
    to_list,
)

type ConfigListFilter = (
    tuple[Literal["id"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["name"], str]
)


class Config(ReloadableObjectFromJson[ConfigInspectResult]):
    """Reloadable Swarm config handle using its originating client settings.

    A context manager attempts removal on exit, including when its body fails,
    and ignores a config that is already gone. Other cleanup failures are grouped
    with any active body exception.
    """

    def __init__(
        self, client_config: ClientConfig, reference: str, is_immutable_id: bool = False
    ) -> None:
        super().__init__(client_config, "id", reference, is_immutable_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with resource_cleanup(exc_val, "Config body and cleanup both failed"):
            try:
                self.remove()
            except NoSuchConfig:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "config", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> ConfigInspectResult:
        return ConfigInspectResult.model_validate(json_object)

    def remove(self) -> None:
        """Remove this config.

        A context manager attempts this removal even if its body raises.
        """
        ConfigCLI(self.client_config).remove(self)

    def exists(self) -> bool:
        """Return whether this config can still be inspected in the Swarm."""
        return ConfigCLI(self.client_config).exists(self.id)

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def version(self) -> DockerObjectVersion | None:
        return self._get_inspect_result().version

    @property
    def created_at(self) -> datetime | None:
        return self._get_inspect_result().created_at

    @property
    def updated_at(self) -> datetime | None:
        return self._get_inspect_result().updated_at

    @property
    def spec(self) -> ConfigSpec | None:
        return self._get_inspect_result().spec


ValidConfig = Config | str


class ConfigCLI(DockerCLICaller):
    """Swarm config commands using the originating client's connection settings."""

    def create(
        self,
        name: str,
        file: str | Path,
        labels: Mapping[str, str] = MappingProxyType({}),
        template_driver: str | None = None,
    ) -> Config:
        """Create a Swarm config from a file.

        See [the docker docs](https://docs.docker.com/engine/swarm/configs/)
        for more information about swarm configs.

        Args:
            name: The config name.
            file: Local file whose contents become the config.
            labels: Metadata labels; the default adds none.
            template_driver: Template driver, or None to omit the option.

        Returns:
            A handle to the newly created config.
        """
        full_cmd = Command([*self.docker_cmd, "config", "create"])
        full_cmd.add_args_iterable_or_single("--label", format_mapping_for_cli(labels))
        full_cmd.add_simple_arg("--template-driver", template_driver)
        full_cmd += [name, file]
        return Config(self.client_config, run(full_cmd), is_immutable_id=True)

    def exists(self, x: str) -> bool:
        """Return whether a config can be inspected.

        Args:
            x: Config name or ID to inspect.

        Returns:
            False when Docker reports NoSuchConfig; True on successful inspection.
            Other inspection failures propagate, including NotASwarmManager.
        """
        try:
            self.inspect(x)
        except NoSuchConfig:
            return False
        else:
            return True

    @overload
    def inspect(self, x: str) -> Config: ...

    @overload
    def inspect(self, x: builtins.list[str]) -> builtins.list[Config]: ...

    @overload
    def inspect(self, x: str | builtins.list[str], *, pretty: Literal[True]) -> str: ...

    def inspect(
        self, x: str | builtins.list[str], *, pretty: bool = False
    ) -> Config | builtins.list[Config] | str:
        """Inspect one or more Swarm configs by name or ID.

        Args:
            x: A name or ID, or a list of references. An empty list does not invoke
                the CLI.
            pretty: Return Docker's human-friendly text instead of handles. The
                text isn't parsed, and no handle or cache snapshot is created.

        Returns:
            One handle for a string, otherwise handles in request order with
            duplicates retained as distinct handles. All snapshots are validated
            before any returned handle is hydrated. With `pretty=True`, the CLI
            text for all references, or an empty string for an empty list.

        Raises:
            ParsingError: If the response cannot be associated unambiguously with
                the requested references or contains inconsistent snapshots.
        """
        references = to_list(x)
        if not references:
            return "" if pretty else []
        if pretty:
            return run([*self.docker_cmd, "config", "inspect", "--pretty", *references])
        json_str = run([*self.docker_cmd, "config", "inspect", *references])
        parser = Config(self.client_config, references[0], is_immutable_id=True)
        snapshots = parse_swarm_inspect_results(parser, json_str, references)
        resources = []
        for identity, snapshot in snapshots:
            resource = Config(self.client_config, identity, is_immutable_id=True)
            resource._set_inspect_result(snapshot)
            resources.append(resource)
        match x:
            case str():
                return resources[0]
            case builtins.list():
                return resources
            case unreachable:
                assert_never(unreachable)

    def list(
        self,
        filters: Mapping[str, str] | Iterable[ConfigListFilter] = MappingProxyType({}),
        *,
        hydrate: bool = False,
    ) -> builtins.list[Config]:
        """List configs available in the Swarm.

        Args:
            filters: Docker filter names and values, either as a mapping or as
                an iterable of ``(key, value)`` pairs. Pairs keep their order
                and repeated keys, and an iterator is read once. The documented
                keys are `id`, `label`, and `name`. The default selects all
                configs. For example, `filters={"name": "app"}` or
                `filters=[("label", "tier=web"), ("label", "team=core")]`.
            hydrate: If `True`, inspect the listed IDs in bounded batches, so a
                listing costs one launch plus one per chunk and reading
                properties costs none. Snapshots stay pinned until `reload()`
                or a mutating call on the handle, and configs removed before
                their inspect are omitted. If `False`, each handle inspects
                lazily on first property access.

        Returns:
            Config handles in CLI output order.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
            ParsingError: A hydration response is malformed or does not match
                the listed IDs.
        """
        filter_args = format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "config", "list", "--quiet"])
        full_cmd.add_args_iterable_or_single("--filter", filter_args)
        output = run(full_cmd)
        ids = output.splitlines()
        configs = [Config(self.client_config, id_, is_immutable_id=True) for id_ in ids]
        if not hydrate:
            return configs
        return hydrate_handles(
            configs,
            lambda ids: run([*self.docker_cmd, "config", "inspect", *ids]),
            NoSuchConfig,
        )

    def remove(self, x: ValidConfig | Iterable[ValidConfig]) -> None:
        """Remove one or more configs.

        Args:
            x: Config names, IDs, or handles.
                An empty selection means the function call does nothing.
        """
        configs = to_list(x)
        if not configs:
            return
        full_cmd = Command([*self.docker_cmd, "config", "rm"])
        full_cmd += configs
        try:
            run(full_cmd)
        finally:
            release_pins(configs)
