"""Swarm stack deployment and discovery through the configured Docker CLI."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, override

import pywhales.components.service.cli_wrapper
import pywhales.components.task.cli_wrapper
from pywhales.client_config import ClientConfig, Command, DockerCLICaller
from pywhales.utils import (
    ValidPath,
    format_filters_for_cli,
    read_env_files,
    run,
    to_list,
    validate_timeout,
)

type StackPsFilter = (
    tuple[Literal["desired-state"], str]
    | tuple[Literal["id"], str]
    | tuple[Literal["name"], str]
    | tuple[Literal["node"], str]
)


class Stack:
    """Named stack handle delegating operations to its originating client.

    Attributes:
        client_config: Shared mutable client configuration used for each operation.
        name: Stack name used in subsequent commands.
    """

    def __init__(self, client_config: ClientConfig, name: str) -> None:
        self.client_config = client_config
        self.name = name

    @override
    def __str__(self) -> str:
        return self.name

    @override
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Stack):
            return self.client_config == other.client_config and self.name == other.name
        else:
            return False

    @override
    def __repr__(self) -> str:
        return f"pywhales.Stack(name='{self.name}')"

    def remove(self, *, detach: bool | None = None) -> None:
        """Remove this stack through the configured Docker CLI.

        See [`docker.stack.remove`](../sub-commands/stack.md#remove) for the
        `detach` argument.
        """
        StackCLI(self.client_config).remove(self, detach=detach)

    def ps(
        self, *, filters: Mapping[str, str] | Iterable[StackPsFilter] | None = None
    ) -> list[pywhales.components.task.cli_wrapper.Task]:
        """Return task handles associated with this stack.

        See [`docker.stack.ps`](../sub-commands/stack.md#ps) for the `filters`
        argument.
        """
        return StackCLI(self.client_config).ps(self, filters=filters)

    def services(
        self,
        *,
        filters: Mapping[str, str]
        | Iterable[pywhales.components.service.cli_wrapper.ServiceListFilter]
        | None = None,
    ) -> list[pywhales.components.service.cli_wrapper.Service]:
        """Return service handles associated with this stack.

        See [`docker.stack.services`](../sub-commands/stack.md#services) for the
        `filters` argument.
        """
        return StackCLI(self.client_config).services(self, filters=filters)


ValidStack = str | Stack


class StackCLI(DockerCLICaller):
    """Stack administration against a Swarm manager, with local deployment files."""

    def config(
        self,
        compose_files: ValidPath | Sequence[ValidPath],
        *,
        skip_interpolation: bool = False,
    ) -> str:
        """Return the final stack configuration after merges and interpolation.

        This runs `docker stack config`, which only reads the Compose files and
        prints the merged result. It changes nothing and needs no Swarm manager.

        Args:
            compose_files: One or more Compose file paths, merged in order. A
                `"-"` entry makes Docker read that file from standard input.
            skip_interpolation: Print only the merged configuration, leaving
                `${VARIABLE}` references unexpanded.

        Returns:
            The configuration YAML text printed by Docker.
        """
        full_cmd = Command([*self.docker_cmd, "stack", "config"])
        full_cmd.add_args_iterable_or_single("--compose-file", compose_files)
        full_cmd.add_flag("--skip-interpolation", skip_interpolation)
        return run(full_cmd)

    def deploy(
        self,
        name: str,
        compose_files: ValidPath | Sequence[ValidPath] = (),
        prune: bool = False,
        resolve_image: str = "always",
        with_registry_auth: bool = False,
        env_files: Sequence[ValidPath] = (),
        variables: Mapping[str, str] = MappingProxyType({}),
        *,
        detach: bool | None = None,
        quiet: bool = False,
        timeout: float | None = None,
    ) -> Stack:
        """Deploy a stack from Compose files.

        This changes daemon-managed services and displays Docker's stdout.
        Failure or local interruption does not undo daemon-side mutations.

        Args:
            name: The name of the stack to deploy. Mandatory.
            compose_files: One or more docker-compose files. If there are more than
                one, they will be fused together.
            prune: Prune services that are no longer referenced
            resolve_image: Query the registry to resolve image digest
                and supported platforms `"always"|"changed"|"never"` (default `"always"`).
                Note that if the registry cannot be queried when using `"always"`, it's
                going to try to use images present locally on the nodes.
            with_registry_auth: Send registry authentication details to Swarm agents.
                Required if you need to run `docker login` to pull the docker images
                in your stack.
            env_files: Similar to `.env` files in docker-compose. Loads `variables` from
                `.env` files. If both `env_files` and `variables` are used, `variables`
                have priority. This behavior is similar to the one you would experience with
                compose.
            variables: Environment overrides for interpolating Compose file
                variables, taking precedence over values loaded from `env_files`.
            detach: `True` sends `--detach` and `False` sends `--detach=false`,
                so Docker waits for the stack services to converge. None omits
                the option and keeps Docker's own default.
            quiet: Suppress Docker's progress output.
            timeout: Positive finite seconds from the deploy CLI's own launch,
                replacing the client default; None inherits it. It is validated
                before the executable is resolved or any env file is read.

        Returns:
            A named stack handle after the deploy CLI exits, not a guarantee that
            all deployed service tasks have converged.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the CLI exceeds its deadline. It is killed and reaped,
                without undoing the deploy or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "stack", "deploy"])

        full_cmd.add_args_iterable_or_single("--compose-file", compose_files)
        full_cmd.add_flag("--prune", prune)
        full_cmd.add_simple_arg("--resolve-image", resolve_image)
        full_cmd.add_flag("--with-registry-auth", with_registry_auth)
        _add_tristate_flag(full_cmd, "--detach", detach)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.append(name)

        env = read_env_files([Path(x) for x in env_files])
        env.update(variables)

        run(full_cmd, capture_stdout=False, env=env, timeout=timeout)
        return Stack(self.client_config, name)

    def list(self) -> builtins.list[Stack]:
        """List stacks using the configured Docker CLI.

        Returns:
            Stack handles in CLI output order.
        """
        full_cmd = Command([*self.docker_cmd, "stack", "ls", "--format", "{{.Name}}"])
        stacks_names = run(full_cmd).splitlines()
        return [Stack(self.client_config, name) for name in stacks_names]

    def ps(
        self,
        x: ValidStack,
        *,
        filters: Mapping[str, str] | Iterable[StackPsFilter] | None = None,
    ) -> builtins.list[pywhales.components.task.cli_wrapper.Task]:
        """List Swarm tasks associated with a stack.

        Args:
            x: Stack name or handle to list tasks for.
            filters: Docker task filter names and values, either as a mapping or
                as an iterable of ``(key, value)`` pairs. Pairs keep their order
                and repeated keys, and an iterator is read once. The documented
                keys are `desired-state`, `id`, `name`, and `node`. Docker
                combines repeated filters as an OR. None adds no filter.

        Returns:
            Task handles in CLI output order, including any task history Docker lists.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
        """
        filter_args = [] if filters is None else format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "stack", "ps", "--quiet", "--no-trunc"])
        full_cmd.add_args_iterable("--filter", filter_args)
        full_cmd.append(x)

        ids = run(full_cmd).splitlines()
        return [
            pywhales.components.task.cli_wrapper.Task(
                self.client_config, id_, is_immutable_id=True
            )
            for id_ in ids
        ]

    def remove(
        self, x: ValidStack | Iterable[ValidStack], *, detach: bool | None = None
    ) -> None:
        """Remove one or more stacks.

        Args:
            x: One or more stacks, an empty selection means nothing will be done.
            detach: `True` sends `--detach` and `False` sends `--detach=false`,
                so Docker waits until the stack removal completes. None omits the
                option and keeps Docker's own default.
        """
        stacks = to_list(x)
        if not stacks:
            return
        full_cmd = Command([*self.docker_cmd, "stack", "remove"])
        _add_tristate_flag(full_cmd, "--detach", detach)
        full_cmd += stacks
        run(full_cmd)

    def services(
        self,
        stack: ValidStack,
        *,
        filters: Mapping[str, str]
        | Iterable[pywhales.components.service.cli_wrapper.ServiceListFilter]
        | None = None,
    ) -> builtins.list[pywhales.components.service.cli_wrapper.Service]:
        """List the services present in the stack.

        Args:
            stack: A docker stack or the name of a stack.
            filters: Docker service filter names and values, either as a mapping
                or as an iterable of ``(key, value)`` pairs. Pairs keep their
                order and repeated keys, and an iterator is read once. The
                supported keys are `id`, `label`, `mode`, and `name`. Docker
                combines repeated filters as an OR. None adds no filter.

        Returns:
            Service handles in CLI output order, inspected individually to resolve
            their full IDs.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
        """
        filter_args = [] if filters is None else format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "stack", "services", "--quiet"])
        full_cmd.add_args_iterable("--filter", filter_args)
        full_cmd.append(stack)
        ids = run(full_cmd).splitlines()
        return [
            pywhales.components.service.cli_wrapper.Service(self.client_config, id_)
            for id_ in ids
        ]


def _add_tristate_flag(command: Command, flag: str, value: bool | None) -> None:
    """Append `flag` for True, `flag=false` for False, and nothing for None."""
    if value is None:
        return
    command.append(flag if value else f"{flag}=false")
