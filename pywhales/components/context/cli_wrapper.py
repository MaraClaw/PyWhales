"""Local Docker CLI context metadata, endpoint configuration, and handles."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self, overload, override

from pydantic import JsonValue

from pywhales._inspect_validation import parse_namespace_inspect_results
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.context.models import (
    ContextEndpoint,
    ContextInspectResult,
    ContextStorage,
)
from pywhales.exceptions import NoSuchContext
from pywhales.utils import ValidPath, run, to_list, validate_timeout


class Context(ReloadableObjectFromJson[ContextInspectResult, str | None]):
    """Reloadable Docker context identified by its name.

    Context-manager exit attempts forced removal, even if the body raises.
    Body and cleanup failures are preserved together when both occur.
    """

    def __init__(
        self,
        client_config: ClientConfig,
        reference: str | None,
        is_immutable_id: bool = False,
    ) -> None:
        super().__init__(client_config, "name", reference, is_immutable_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with resource_cleanup(exc_val, "Context body and cleanup both failed"):
            self.remove(force=True)

    @override
    def _fetch_inspect_result_json(self, reference: str | None) -> dict[str, JsonValue]:
        full_cmd = Command([*self.docker_cmd, "context", "inspect"])
        if reference is not None:
            full_cmd.append(reference)
        json_str = run(full_cmd)
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> ContextInspectResult:
        return ContextInspectResult.model_validate(json_object)

    @property
    def name(self) -> str | None:
        return self._get_immutable_id()

    @property
    def metadata(self) -> dict[str, JsonValue] | None:
        return self._get_inspect_result().metadata

    @property
    def endpoints(self) -> dict[str, ContextEndpoint] | None:
        return self._get_inspect_result().endpoints

    @property
    def tls_material(self) -> dict[str, JsonValue] | None:
        return self._get_inspect_result().tls_material

    @property
    def storage(self) -> ContextStorage | None:
        return self._get_inspect_result().storage

    def remove(self, force: bool = False) -> None:
        """Remove this context, optionally forcing removal of the current context.

        Args:
            force: Forward Docker's forced-removal option when True.
        """
        ContextCLI(self.client_config).remove(self, force)

    def exists(self) -> bool:
        """Return whether this context can still be inspected."""
        return ContextCLI(self.client_config).exists(str(self))

    def update(
        self,
        description: str | None = None,
        docker: dict[str, Any] | DockerContextConfig | None = None,
    ) -> None:
        """Update this context's description or Docker endpoint, then reload it.

        See `ContextCLI.update` for the option semantics. The cached snapshot is
        replaced by a fresh inspection once the update succeeds.

        Args:
            description: New description, or None to leave it unchanged.
            docker: New Docker endpoint options, or None to leave them unchanged.
        """
        ContextCLI(self.client_config).update(self, description, docker)
        self.reload()

    def use(self) -> None:
        """Set this context as Docker's default context."""
        ContextCLI(self.client_config).use(self)

    @override
    def __repr__(self) -> str:
        return f"pywhales.Context(name='{self.name}', endpoints={self.endpoints})"


ValidContext = Context | str


@dataclass
class DockerContextConfig:
    """Mutable Docker endpoint options for creating a context."""

    from_: ValidContext | None = None
    host: str | None = None
    certificate_authority: Path | str | None = None
    certificate: Path | str | None = None
    key: Path | str | None = None
    skip_tls_verify: bool = False

    def format_for_docker_cli(self) -> str:
        """Return comma-separated endpoint options, omitting None-valued fields."""
        list_of_args = []
        if self.from_ is not None:
            list_of_args.append(f"from={self.from_}")
        if self.host is not None:
            list_of_args.append(f"host={self.host}")
        if self.certificate_authority is not None:
            list_of_args.append(f"ca={self.certificate_authority}")
        if self.certificate is not None:
            list_of_args.append(f"cert={self.certificate}")
        if self.key is not None:
            list_of_args.append(f"key={self.key}")
        list_of_args.append(f"skip-tls-verify={self.skip_tls_verify}")

        return ",".join(list_of_args)


@dataclass
class KubernetesContextConfig:
    """Mutable Kubernetes endpoint options, no longer accepted by Docker CLI 29.

    `ContextCLI.create` rejects any Kubernetes configuration.
    """

    from_: ValidContext | None = None
    config_file: str | Path | None = None
    context_override: str | None = None
    namespace_override: str | None = None

    def format_for_docker_cli(self) -> str:
        """Return comma-separated endpoint options, omitting None-valued fields."""
        list_of_args = []
        if self.from_ is not None:
            list_of_args.append(f"from={self.from_}")
        if self.config_file is not None:
            list_of_args.append(f"config-file={self.config_file}")
        if self.context_override is not None:
            list_of_args.append(f"context-override={self.context_override}")
        if self.namespace_override is not None:
            list_of_args.append(f"namespace-override={self.namespace_override}")
        return ",".join(list_of_args)


def _docker_endpoint_option(
    docker: dict[str, Any] | DockerContextConfig | None,
) -> str | None:
    """Return the `--docker` option value, or None when no endpoint is set."""
    if docker is None:
        return None
    if isinstance(docker, dict):
        docker = DockerContextConfig(**docker)
    return docker.format_for_docker_cli()


def _require_archive_file(path: ValidPath) -> None:
    """Reject `-`, which Docker reads as standard input or output.

    PyWhales captures a command's output as text and supplies no input, so a
    context archive can only travel through a named file.

    Raises:
        ValueError: If the path is `-`.
    """
    if str(path) == "-":
        raise ValueError("A context archive must be a file, not a standard stream")


class ContextCLI(DockerCLICaller):
    """Context commands for local CLI configuration, not daemon resources."""

    def create(
        self,
        context_name: str,
        default_stack_orchestrator: str | None = None,
        description: str | None = None,
        from_: ValidContext | None = None,
        docker: dict[str, Any] | DockerContextConfig | None = None,
        kubernetes: dict[str, Any] | KubernetesContextConfig | None = None,
    ) -> Context:
        """Create and inspect a Docker context.

        Args:
            context_name: Name for the new context.
            default_stack_orchestrator: Retained for positional compatibility;
                must be None because Docker CLI 29 removed stack orchestrators.
            description: Context description, or None to omit the option.
            from_: Existing context to copy from, or None to omit the option.
            docker: Docker endpoint options; None omits endpoint configuration.
            kubernetes: Retained for positional compatibility; must be None
                because Docker CLI 29 removed Kubernetes contexts.

        Returns:
            A freshly inspected handle to the created context.

        Raises:
            ValueError: If `default_stack_orchestrator` or `kubernetes` is not
                None. Raised before the Docker executable is looked up.
        """
        if default_stack_orchestrator is not None or kubernetes is not None:
            raise ValueError(
                "Docker CLI 29 removed Kubernetes contexts and stack orchestrators"
            )

        full_cmd = Command([*self.docker_cmd, "context", "create"])

        full_cmd.add_simple_arg("--description", description)
        full_cmd.add_simple_arg("--from", from_)
        full_cmd.add_simple_arg("--docker", _docker_endpoint_option(docker))
        full_cmd.append(context_name)
        run(full_cmd)
        return self.inspect(context_name)

    def exists(self, x: str) -> bool:
        """Return whether a context can be inspected.

        Args:
            x: Context name to inspect.

        Returns:
            False when Docker reports NoSuchContext; True on successful inspection.
            Other inspection failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchContext:
            return False
        else:
            return True

    def export(
        self,
        context: ValidContext,
        output: ValidPath | None = None,
        *,
        timeout: float | None = None,
    ) -> None:
        """Export a context to a tar archive file.

        Args:
            context: Context handle or name to export.
            output: Archive file to write. None lets the CLI choose its default
                name, `<context>.dockercontext`, in the CLI process's working
                directory. `-` is rejected, because standard output is never
                returned.
            timeout: Positive finite seconds for the local CLI, or None to
                inherit the client default.

        Raises:
            ValueError: If `timeout` is not positive and finite, or `output` is
                `-`. Raised before the Docker executable is looked up.
        """
        validate_timeout(timeout)
        if output is not None:
            _require_archive_file(output)
        full_cmd = Command([*self.docker_cmd, "context", "export", context])
        if output is not None:
            full_cmd.append(output)
        run(full_cmd, timeout=timeout)

    def import_(
        self, name: str, source: ValidPath, *, timeout: float | None = None
    ) -> Context:
        """Import a context from a tar or zip file and inspect it.

        Args:
            name: Name for the imported context.
            source: Archive file to read. `-` is rejected, because standard input
                is never supplied.
            timeout: Positive finite seconds for the import CLI, or None to
                inherit the client default. The inspection that follows is
                outside this budget.

        Returns:
            A freshly inspected handle to the imported context.

        Raises:
            ValueError: If `timeout` is not positive and finite, or `source` is
                `-`. Raised before the Docker executable is looked up.
        """
        validate_timeout(timeout)
        _require_archive_file(source)
        run(
            Command([*self.docker_cmd, "context", "import", name, source]),
            timeout=timeout,
        )
        return self.inspect(name)

    @overload
    def inspect(self, x: str | None = None) -> Context: ...

    @overload
    def inspect(self, x: builtins.list[str]) -> builtins.list[Context]: ...

    def inspect(
        self, x: str | builtins.list[str] | None = None
    ) -> Context | builtins.list[Context]:
        """Inspect the current context or one or more named contexts.

        Args:
            x: A context name or list of names. None selects the current context;
                an empty list returns without invoking the CLI.

        Returns:
            One handle for a string or None, otherwise handles in request order.
            List results preserve duplicates as distinct handles and validate the
            whole response before hydrating any handle.

        Raises:
            ParsingError: If a batch response has invalid shape, inconsistent
                snapshots, or names that do not exactly match the requests.
        """
        if isinstance(x, str) or x is None:
            return Context(self.client_config, x)
        else:
            if not x:
                return []
            parser = Context(self.client_config, x[0], is_immutable_id=True)
            snapshots = parse_namespace_inspect_results(
                parser, run([*self.docker_cmd, "context", "inspect", *x]), x
            )
            contexts = []
            for identity, snapshot in snapshots:
                context = Context(self.client_config, identity, is_immutable_id=True)
                context._set_inspect_result(snapshot)
                contexts.append(context)
            return contexts

    def list(self) -> builtins.list[Context]:
        """List available Docker contexts.

        Returns:
            Context handles in CLI output order.
        """

        full_cmd = Command([*self.docker_cmd, "context", "list", "--quiet"])

        ids = run(full_cmd).splitlines()
        return [Context(self.client_config, id_, is_immutable_id=True) for id_ in ids]

    def remove(
        self, x: ValidContext | Iterable[ValidContext], force: bool = False
    ) -> None:
        """Remove one or more contexts.

        Args:
            x: One or more contexts, an empty selection means no-op.
            force: Force removal, including the current context.
        """
        contexts = to_list(x)
        if not contexts:
            return
        full_cmd = Command([*self.docker_cmd, "context", "remove"])
        full_cmd.add_flag("--force", force)
        full_cmd += contexts
        run(full_cmd)

    def show(self) -> str:
        """Return the name of the current context.

        Returns:
            The context name Docker would use for this client's configuration,
            without a trailing newline.
        """
        return run(Command([*self.docker_cmd, "context", "show"]))

    def update(
        self,
        context: ValidContext,
        description: str | None = None,
        docker: dict[str, Any] | DockerContextConfig | None = None,
    ) -> Context:
        """Update a context's description or Docker endpoint and inspect it.

        Args:
            context: Context handle or name to update.
            description: New description, or None to omit the option.
            docker: New Docker endpoint options, formatted exactly as in
                `create`; None omits the option.

        Returns:
            A freshly inspected handle to the updated context.
        """
        full_cmd = Command([*self.docker_cmd, "context", "update"])
        full_cmd.add_simple_arg("--description", description)
        full_cmd.add_simple_arg("--docker", _docker_endpoint_option(docker))
        full_cmd.append(context)
        run(full_cmd)
        return self.inspect(str(context))

    def use(self, context: ValidContext) -> Context:
        """Set Docker's default context and inspect it.

        Args:
            context: Context handle or name to select as the default.

        Returns:
            A freshly inspected handle to the selected context.
        """
        full_cmd = Command([*self.docker_cmd, "context", "use", context])
        run(full_cmd)
        return self.inspect(str(context))
