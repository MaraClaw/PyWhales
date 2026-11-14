"""Docker Compose commands using the client's project and connection settings.

Commands require an available Compose plugin. Resource handles retain the
originating configuration; returned output is not diagnostic redaction output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Unpack, assert_never, get_args, overload

from pydantic import BaseModel, JsonValue
from typing_extensions import TypedDict

import pywhales.components.container.cli_wrapper
import pywhales.components.image.cli_wrapper
import pywhales.components.volume.cli_wrapper
from pywhales._environment import container_environment
from pywhales._json import loads_compat
from pywhales.client_config import ClientConfig, Command, DockerCLICaller, ParsingError
from pywhales.components._json_output import JsonOutputParser
from pywhales.components.compose.bridge import ComposeBridgeCLI
from pywhales.components.compose.models import (
    ComposeConfig,
    ComposeEvent,
    ComposeImage,
    ComposeProject,
    ComposeStats,
    ComposeVariable,
    ComposeVolume,
)
from pywhales.components.top import ComposeTopResult, parse_compose_top
from pywhales.enums import (
    ComposeCreatePullPolicy,
    ComposeUpPullPolicy,
)
from pywhales.exceptions import DockerException, DockerTimeout
from pywhales.utils import (
    MappedProcessStream,
    ProcessStream,
    ValidPath,
    custom_parse_object_as,
    format_filters_for_cli,
    format_mapping_for_cli,
    format_port_arg,
    format_signal_arg,
    parse_ls_status_count,
    run,
    stream_stdout_and_stderr,
    to_list,
    validate_timeout,
)

type ComposeLsFilter = tuple[Literal["name"], str]
type ComposeServicePath = tuple[str, ValidPath]
type ComposePsStatus = Literal[
    "paused", "restarting", "removing", "running", "dead", "created", "exited"
]
type ComposeConfigNames = Literal[
    "services", "volumes", "profiles", "images", "networks", "models"
]
type ComposePullPolicyInput = Literal["missing", "always"]

_CONFIG_NAMES = get_args(ComposeConfigNames.__value__)
_COMMITTED_IMAGE = re.compile(r"Committed as (sha256:[0-9a-f]{64})")


def _parse_config(output: str) -> ComposeConfig:
    return ComposeConfig(**loads_compat(output))


def _parse_config_json(output: str) -> dict[str, JsonValue]:
    return custom_parse_object_as(dict[str, JsonValue], loads_compat(output))


def _parse_config_hashes(output: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, digest = line.partition(" ")
        if not (name and separator and digest):
            raise ParsingError("Unexpected compose config hash output")
        hashes[name] = digest
    return hashes


def _parse_config_environment(output: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if not (name and separator):
            raise ParsingError("Unexpected compose config environment output")
        environment[name] = value
    return environment


def _parse_config_variables(output: str) -> list[ComposeVariable]:
    return list(
        custom_parse_object_as(
            dict[str, ComposeVariable], loads_compat(output)
        ).values()
    )


def _parse_images(output: str) -> list[ComposeImage]:
    decoded = loads_compat(output)
    return custom_parse_object_as(
        list[ComposeImage], [] if decoded is None else decoded
    )


def _parse_json_lines[ModelT: BaseModel](
    model: type[ModelT], output: str
) -> list[ModelT]:
    return [model.model_validate(loads_compat(line)) for line in output.splitlines()]


def _add_optional_bool(command: Command, name: str, value: bool | None) -> None:
    """Append `name`, or `name=false`, for a tri-state option; omit it for `None`."""
    if value is not None:
        command.append(name if value else f"{name}=false")


def _whole_seconds(duration: int | timedelta | None) -> int | None:
    """Return integer seconds for a Compose `--timeout`, truncating a timedelta."""
    if isinstance(duration, timedelta):
        return int(duration.total_seconds())
    return duration


def _parse_projects(output: str) -> list[ComposeProject]:
    return [
        ComposeProject(
            name=proj["Name"],
            created=parse_ls_status_count(proj["Status"], "created"),
            running=parse_ls_status_count(proj["Status"], "running"),
            restarting=parse_ls_status_count(proj["Status"], "restarting"),
            exited=parse_ls_status_count(proj["Status"], "exited"),
            paused=parse_ls_status_count(proj["Status"], "paused"),
            dead=parse_ls_status_count(proj["Status"], "dead"),
            config_files=[
                Path(path)
                for path in proj.get("ConfigFiles", "").split(",")
                if "ConfigFiles" in proj
            ]
            or None,
        )
        for proj in loads_compat(output)
    ]


def _parse_event(record: bytes) -> ComposeEvent:
    return ComposeEvent(**loads_compat(record))


class ComposeRunArgs(TypedDict, total=False, closed=True):
    build: bool
    entrypoint: str | None
    envs: Mapping[str, str]
    labels: Mapping[str, str]
    name: str | None
    tty: bool
    dependencies: bool
    publish: Sequence[pywhales.components.container.cli_wrapper.ValidPortMapping]
    remove: bool
    service_ports: bool
    use_aliases: bool
    user: str | None
    volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition]
    workdir: str | Path | None
    cap_add: Iterable[str]
    cap_drop: Iterable[str]
    env_from_files: ValidPath | Iterable[ValidPath]
    interactive: bool | None
    no_recreate: bool
    pull: ComposeUpPullPolicy | None
    quiet: bool
    quiet_build: bool
    quiet_pull: bool
    remove_orphans: bool
    timeout: float | None


class ComposeCLI(DockerCLICaller):
    """Compose project commands with blocking and owned-stream output modes.

    Nonempty streaming commands launch the local CLI before returning. Exhaust
    the stream, use its context manager, or call `close()` on early exit; a bare
    loop break does not release it. Closing kills and reaps the local CLI, not
    daemon-managed services, and does not undo completed changes. Byte-output
    streams yield records with `"stdout"` or `"stderr"` source labels and byte
    payloads; `events()` returns a stream of parsed `ComposeEvent` objects, and
    `stats(stream=True)` returns raw byte records.

    Attributes:
        bridge: The Compose Bridge command group, `docker.compose.bridge`.
    """

    def __init__(self, client_config: ClientConfig) -> None:
        """Retain the shared config and wire the Bridge command group.

        Args:
            client_config: Settings shared with the originating client.
        """
        super().__init__(client_config)
        self.bridge = ComposeBridgeCLI(client_config)

    def attach(
        self,
        service: str,
        *,
        detach_keys: str | None = None,
        index: int | None = None,
        stdin: bool = True,
        sig_proxy: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Attach the caller's terminal streams to a service's running container.

        The local CLI inherits stdin, stdout and stderr and blocks until the
        container stops or the attachment detaches.

        Args:
            service: The service whose running container is attached.
            detach_keys: Override the key sequence for detaching.
            index: Container index when the service has several replicas;
                `None` leaves the CLI default (the first).
            stdin: Attach stdin. `False` passes `--no-stdin`.
            sig_proxy: Proxy received signals to the container process. `True`
                passes `--sig-proxy` and `False` passes `--sig-proxy=false`.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; the container keeps running, and the Compose plugin
                process the CLI started may outlive it.
            DockerException: The local Compose CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "attach"])
        full_cmd.add_simple_arg("--detach-keys", detach_keys)
        full_cmd.add_simple_arg("--index", index)
        full_cmd.add_flag("--no-stdin", not stdin)
        _add_optional_bool(full_cmd, "--sig-proxy", sig_proxy)
        full_cmd.append(service)
        run(full_cmd, tty=True, timeout=timeout)

    @overload
    def build(
        self,
        services: list[str] | str | None = ...,
        build_args: Mapping[str, str] = ...,
        cache: bool = ...,
        progress: str | None = ...,
        pull: bool = ...,
        quiet: bool = ...,
        with_dependencies: bool = ...,
        ssh: str | None = ...,
        *,
        stream_logs: Literal[True],
        builder: str | None = ...,
        check: bool = ...,
        print: bool = ...,
        provenance: bool | str | None = ...,
        sbom: bool | str | None = ...,
        push: bool = ...,
        memory: int | str | None = ...,
        timeout: float | None = None,
    ) -> ProcessStream | None: ...

    @overload
    def build(
        self,
        services: list[str] | str | None = ...,
        build_args: Mapping[str, str] = ...,
        cache: bool = ...,
        progress: str | None = ...,
        pull: bool = ...,
        quiet: bool = ...,
        with_dependencies: bool = ...,
        ssh: str | None = ...,
        stream_logs: Literal[False] = ...,
        *,
        builder: str | None = ...,
        check: bool = ...,
        print: bool = ...,
        provenance: bool | str | None = ...,
        sbom: bool | str | None = ...,
        push: bool = ...,
        memory: int | str | None = ...,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def build(
        self,
        services: list[str] | str | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        cache: bool = True,
        progress: str | None = None,
        pull: bool = False,
        quiet: bool = False,
        with_dependencies: bool = False,
        ssh: str | None = None,
        stream_logs: bool = False,
        *,
        builder: str | None = None,
        check: bool = False,
        print: bool = False,
        provenance: bool | str | None = None,
        sbom: bool | str | None = None,
        push: bool = False,
        memory: int | str | None = None,
        timeout: float | None = None,
    ) -> ProcessStream | None: ...

    def build(
        self,
        services: list[str] | str | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        cache: bool = True,
        progress: str | None = None,
        pull: bool = False,
        quiet: bool = False,
        with_dependencies: bool = False,
        ssh: str | None = None,
        stream_logs: bool = False,
        *,
        builder: str | None = None,
        check: bool = False,
        print: bool = False,
        provenance: bool | str | None = None,
        sbom: bool | str | None = None,
        push: bool = False,
        memory: int | str | None = None,
        timeout: float | None = None,
    ) -> ProcessStream | None:
        """Build services declared in the configured Compose project.

        New flags require a supporting Compose plugin. No version probe or
        automatic downgrade is performed.

        Args:
            services: Services to build. `None` selects all services; `[]` skips
                execution after validating the output-mode combination.
            build_args: Build-time variable names and values for services. Eligible
                values use the child environment, also affecting Compose interpolation
                and name-only service environment entries. Ineligible entries stay
                literal unless strict_env_transport rejects them before lookup.
            cache: Set to `False` if you don't want to use the cache to build your images
            progress: Set type of progress output (auto, tty, plain, json, quiet).
                Overrides `compose_progress` for this call only; `None` uses the
                client setting or the CLI default.
            pull: Set to `True` to always attempt to pull a newer version of the
                image (in the `FROM` statements for example).
            quiet: Don't print anything
            with_dependencies: Also build dependencies (transitively)
            ssh: Set SSH authentications used when building service images.
                (use `'default'` for using your default SSH Agent)
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for the CLI to complete.
            builder: Select the builder used for this build.
            check: Check the build configuration instead of building images.
            print: Print the equivalent Bake definition instead of building.
                Output follows the usual convention: blocking calls print to stdout
                and return `None`; `stream_logs=True` returns the output stream.
            provenance: Add a provenance attestation (`True`), disable it (`False`),
                or pass attestation parameters such as `"mode=max"`.
            sbom: Add an SBOM attestation (`True`), disable it (`False`), or pass
                attestation parameters. `None` leaves the CLI default unchanged.
            push: Push the built service images.
            memory: Memory limit for the build container, in bytes or as a size
                such as `"512m"`. The CLI doesn't support it with BuildKit.
                `None` omits the option.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated even for an empty selection.
                A stream's deadline advances while consumption pauses.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected. Otherwise `None`, including for `[]`.
            `print=True` does not change this result shape.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`, timeout is
                invalid, or strict_env_transport rejects a literal assignment.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped. Daemon-side build work is not guaranteed to stop.
        """
        validate_timeout(timeout)
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )

        if services == []:
            return
        build_arguments, child_env = container_environment(
            build_args, self.client_config
        )
        full_cmd = Command(
            [*self.client_config.get_compose_command(progress=progress), "build"]
        )
        full_cmd.add_args_iterable("--build-arg", build_arguments)
        full_cmd.add_flag("--no-cache", not cache)
        full_cmd.add_flag("--pull", pull)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.add_simple_arg("--ssh", ssh)
        full_cmd.add_flag("--with-dependencies", with_dependencies)
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_simple_arg("--memory", memory)
        full_cmd.add_flag("--check", check)
        full_cmd.add_flag("--print", print)
        full_cmd.add_flag("--push", push)
        for flag, attestation in (("--provenance", provenance), ("--sbom", sbom)):
            match attestation:
                case None:
                    continue
                case bool():
                    full_cmd.append(f"{flag}={str(attestation).lower()}")
                case str():
                    full_cmd.append(f"{flag}={attestation}")
                case unreachable:
                    assert_never(unreachable)

        if services is not None:
            full_cmd += to_list(services)
        else:
            pass  # passing nothing means all services are built
        if stream_logs:
            return stream_stdout_and_stderr(full_cmd, env=child_env, timeout=timeout)
        else:
            run(full_cmd, env=child_env, capture_stdout=False, timeout=timeout)

    @overload
    def config(
        self,
        return_json: Literal[False] = ...,
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: None = ...,
        quiet: Literal[False] = ...,
    ) -> ComposeConfig: ...

    @overload
    def config(
        self,
        return_json: Literal[True],
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: None = ...,
        quiet: Literal[False] = ...,
    ) -> dict[str, JsonValue]: ...

    @overload
    def config(
        self,
        return_json: bool = ...,
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: None = ...,
        quiet: Literal[False] = ...,
    ) -> ComposeConfig | dict[str, JsonValue]: ...

    @overload
    def config(
        self,
        return_json: bool = ...,
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: ValidPath | None = ...,
        quiet: Literal[True],
    ) -> None: ...

    @overload
    def config(
        self,
        return_json: bool = ...,
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: ValidPath,
        quiet: bool = ...,
    ) -> None: ...

    @overload
    def config(
        self,
        return_json: bool = ...,
        *,
        no_interpolate: bool = ...,
        no_normalize: bool = ...,
        no_path_resolution: bool = ...,
        no_consistency: bool = ...,
        no_env_resolution: bool = ...,
        lock_image_digests: bool = ...,
        resolve_image_digests: bool = ...,
        output: ValidPath | None = ...,
        quiet: bool = ...,
    ) -> ComposeConfig | dict[str, JsonValue] | None: ...

    def config(
        self,
        return_json: bool = False,
        *,
        no_interpolate: bool = False,
        no_normalize: bool = False,
        no_path_resolution: bool = False,
        no_consistency: bool = False,
        no_env_resolution: bool = False,
        lock_image_digests: bool = False,
        resolve_image_digests: bool = False,
        output: ValidPath | None = None,
        quiet: bool = False,
    ) -> ComposeConfig | dict[str, JsonValue] | None:
        """Return the resolved Compose project configuration.

        The typed model covers the fields of a normalized, interpolated
        project. With render modifiers such as `no_interpolate` or
        `no_normalize`, use `return_json=True`, because values can leave the
        shapes the typed model declares.

        Args:
            return_json: Preserve all JSON fields in a dictionary when `True`;
                otherwise parse the fields supported by the typed model.
            no_interpolate: Don't interpolate environment variables.
            no_normalize: Don't normalize the Compose model.
            no_path_resolution: Don't resolve file paths.
            no_consistency: Don't check model consistency. The output can be
                invalid Compose.
            no_env_resolution: Don't resolve service environment files.
            lock_image_digests: Produce an override document holding image
                digests rather than the full project.
            resolve_image_digests: Pin image tags to digests, which needs
                registry access.
            output: Save the rendering to this file on the Python host instead
                of returning it.
            quiet: Only validate the configuration and print nothing.

        Returns:
            The typed configuration by default, or the complete decoded JSON
            dictionary when `return_json=True`. `None` when `quiet=True` or
            `output` is given: nothing is read back or parsed.

        Raises:
            ParsingError: If the output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
            DockerException: The configuration is invalid, including when only
                validating with `quiet=True`.
        """
        full_cmd = Command([*self.docker_compose_cmd, "config", "--format", "json"])
        full_cmd.add_flag("--no-interpolate", no_interpolate)
        full_cmd.add_flag("--no-normalize", no_normalize)
        full_cmd.add_flag("--no-path-resolution", no_path_resolution)
        full_cmd.add_flag("--no-consistency", no_consistency)
        full_cmd.add_flag("--no-env-resolution", no_env_resolution)
        full_cmd.add_flag("--lock-image-digests", lock_image_digests)
        full_cmd.add_flag("--resolve-image-digests", resolve_image_digests)
        full_cmd.add_simple_arg("--output", output)
        full_cmd.add_flag("--quiet", quiet)
        result = run(full_cmd, capture_stdout=True)
        if quiet or output is not None:
            return None
        parser = JsonOutputParser("compose config", self.client_config.raw_diagnostics)
        if return_json:
            return parser.parse(_parse_config_json, result)
        else:
            return parser.parse(_parse_config, result)

    def config_names(self, kind: ComposeConfigNames) -> list[str]:
        """Return the names of one kind of project resource.

        Args:
            kind: Resource kind to list: `"services"`, `"volumes"`,
                `"profiles"`, `"images"`, `"networks"` or `"models"`.

        Returns:
            One name per output line, in CLI order. Duplicates are kept: two
            services using one image list it twice. Empty when there are none.

        Raises:
            ValueError: `kind` isn't one of the listed kinds. Raised before the
                CLI is resolved.
            DockerException: The project configuration is invalid.
        """
        if kind not in _CONFIG_NAMES:
            raise ValueError("kind must be a supported Compose config listing")
        return run(
            Command([*self.docker_compose_cmd, "config", f"--{kind}"])
        ).splitlines()

    def config_hashes(
        self, services: str | Sequence[str] | None = None
    ) -> dict[str, str]:
        """Return the configuration hash Compose computes for each service.

        Args:
            services: Services to hash. `None` and an empty sequence select every
                service, as in `ps()`.

        Returns:
            Hashes keyed by service name, in CLI order.

        Raises:
            ParsingError: A line isn't `<service> <hash>`.
            DockerException: The project configuration is invalid or a service
                is unknown.
        """
        selected = [] if services is None else to_list(services)
        full_cmd = Command([*self.docker_compose_cmd, "config"])
        full_cmd.add_simple_arg("--hash", ",".join(selected) or "*")
        return _parse_config_hashes(run(full_cmd))

    def config_variables(self) -> list[ComposeVariable]:
        """Return the interpolation variables the project references.

        Returns:
            One entry per variable, in CLI order, with its default, alternate
            value and whether it is required. Empty when the project uses none.

        Raises:
            ParsingError: The output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command(
            [*self.docker_compose_cmd, "config", "--variables", "--format", "json"]
        )
        parser = JsonOutputParser(
            "compose config variables", self.client_config.raw_diagnostics
        )
        return parser.parse(_parse_config_variables, run(full_cmd))

    def config_environment(self) -> dict[str, str]:
        """Return the environment Compose uses for interpolation.

        The result includes every ambient variable the CLI inherits (see
        `inherited_environment_names`) as well as values from environment files,
        so it can hold secrets. A value spanning several lines can't be told
        apart from following variables: a continuation line without `=` raises
        `ParsingError`, and one containing `=` is read as another variable.

        Returns:
            Variable names and values.

        Raises:
            ParsingError: A line has no `=` separator.
        """
        full_cmd = Command([*self.docker_compose_cmd, "config", "--environment"])
        return _parse_config_environment(run(full_cmd))

    @overload
    def create(
        self,
        services: str | list[str] | None = ...,
        build: bool = ...,
        force_recreate: bool = ...,
        no_build: bool = ...,
        no_recreate: bool = ...,
        pull: ComposeCreatePullPolicy | None = ...,
        *,
        stream_logs: Literal[True],
        quiet_pull: bool = ...,
        remove_orphans: bool = ...,
        scales: Mapping[str, int] = ...,
        yes: bool = ...,
    ) -> ProcessStream | None: ...

    @overload
    def create(
        self,
        services: str | list[str] | None = ...,
        build: bool = ...,
        force_recreate: bool = ...,
        no_build: bool = ...,
        no_recreate: bool = ...,
        pull: ComposeCreatePullPolicy | None = ...,
        stream_logs: Literal[False] = ...,
        *,
        quiet_pull: bool = ...,
        remove_orphans: bool = ...,
        scales: Mapping[str, int] = ...,
        yes: bool = ...,
    ) -> None: ...

    @overload
    def create(
        self,
        services: str | list[str] | None = None,
        build: bool = False,
        force_recreate: bool = False,
        no_build: bool = False,
        no_recreate: bool = False,
        pull: ComposeCreatePullPolicy | None = None,
        stream_logs: bool = False,
        *,
        quiet_pull: bool = False,
        remove_orphans: bool = False,
        scales: Mapping[str, int] = MappingProxyType({}),
        yes: bool = False,
    ) -> ProcessStream | None: ...

    def create(
        self,
        services: str | list[str] | None = None,
        build: bool = False,
        force_recreate: bool = False,
        no_build: bool = False,
        no_recreate: bool = False,
        pull: ComposeCreatePullPolicy | None = None,
        stream_logs: bool = False,
        *,
        quiet_pull: bool = False,
        remove_orphans: bool = False,
        scales: Mapping[str, int] = MappingProxyType({}),
        yes: bool = False,
    ) -> ProcessStream | None:
        """Create service containers without starting them.

        Args:
            services: The name of the services for which the containers will
                be created. The default `None` means that the containers for all
                services will be created. A single string means we will create the
                container for a single service. A list of string means we will create
                the containers for each service in the list. An empty list means nothing
                will be created, the function call is then a no-op.
            build: Build images before creating containers.
            force_recreate: Recreate containers even if their configuration and
                image haven't changed.
            no_build: Don't build an image, even if it's missing.
            no_recreate: If containers already exist, don't recreate them.
                Incompatible with `force_recreate=True`.
            pull: Pull image before running ("always"|"missing"|"never"|"build").
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.
            quiet_pull: Pull images without printing progress information.
            remove_orphans: Remove containers for services not defined in the
                Compose file.
            scales: Instance counts overriding the Compose file, such as
                `{"web": 2}`; emitted as one `--scale` per entry.
            yes: Assume "yes" to all prompts and run non-interactively.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.
        """
        full_cmd = Command([*self.docker_compose_cmd, "create"])
        full_cmd.add_flag("--build", build)
        full_cmd.add_flag("--force-recreate", force_recreate)
        full_cmd.add_flag("--no-build", no_build)
        full_cmd.add_flag("--no-recreate", no_recreate)
        full_cmd.add_simple_arg("--pull", pull)
        full_cmd.add_flag("--quiet-pull", quiet_pull)
        full_cmd.add_flag("--remove-orphans", remove_orphans)
        full_cmd.add_args_iterable("--scale", format_mapping_for_cli(scales))
        full_cmd.add_flag("--yes", yes)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        if stream_logs:
            if self.client_config.raw_diagnostics:
                print(full_cmd)
            return stream_stdout_and_stderr(full_cmd)
        else:
            run(full_cmd, capture_stdout=False)

    @overload
    def down(
        self,
        services: list[str] | str | None = ...,
        remove_orphans: bool = ...,
        remove_images: str | None = ...,
        timeout: int | None = ...,
        volumes: bool = ...,
        quiet: bool = ...,
        *,
        stream_logs: Literal[True],
    ) -> ProcessStream | None: ...

    @overload
    def down(
        self,
        services: list[str] | str | None = ...,
        remove_orphans: bool = ...,
        remove_images: str | None = ...,
        timeout: int | None = ...,
        volumes: bool = ...,
        quiet: bool = ...,
        stream_logs: Literal[False] = ...,
    ) -> None: ...

    @overload
    def down(
        self,
        services: list[str] | str | None = None,
        remove_orphans: bool = False,
        remove_images: str | None = None,
        timeout: int | None = None,
        volumes: bool = False,
        quiet: bool = False,
        stream_logs: bool = False,
    ) -> ProcessStream | None: ...

    def down(
        self,
        services: list[str] | str | None = None,
        remove_orphans: bool = False,
        remove_images: str | None = None,
        timeout: int | None = None,
        volumes: bool = False,
        quiet: bool = False,
        stream_logs: bool = False,
    ) -> ProcessStream | None:
        """Stop and remove project containers and selected related resources.

        Args:
            services: The services to stop. If `None` (default), all services are
                stopped. If an empty list is provided, the function call does nothing, it's
                a no-op.
            remove_orphans: Remove containers for services not defined in
                the Compose file.
            remove_images: Remove images used by services.
                `"local"` remove only images that don't have a custom
                tag. Possible values are `"local"` and `"all"`.
            timeout: Container shutdown grace period in seconds. `None` leaves
                the Compose default unchanged; this is not a local CLI deadline.
            volumes: Remove named volumes declared in the
                volumes section of the Compose file and anonymous
                volumes attached to containers.
            quiet: If `False`, send to stderr and stdout the progress spinners with
                the messages. If `True`, do not display anything.
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`.
        """
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )

        full_cmd = Command([*self.docker_compose_cmd, "down"])
        full_cmd.add_flag("--remove-orphans", remove_orphans)
        full_cmd.add_simple_arg("--rmi", remove_images)
        full_cmd.add_simple_arg("--timeout", timeout)
        full_cmd.add_flag("--volumes", volumes)

        if services == []:
            return
        elif services is not None:
            services = to_list(services)
            full_cmd += services

        if stream_logs:
            return stream_stdout_and_stderr(full_cmd)
        else:
            run(full_cmd, capture_stderr=quiet, capture_stdout=quiet)

    def execute(
        self,
        service: str,
        command: list[str],
        detach: bool = False,
        envs: Mapping[str, str] = MappingProxyType({}),
        index: int = 1,
        tty: bool = True,
        privileged: bool = False,
        user: str | None = None,
        workdir: str | Path | None = None,
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Execute a command in a running container.

        Args:
            service: The name of the service.
            command: The command to execute.
            detach: Let the container command continue after the local CLI exits.
                Detached calls return `None`, not a container handle.
            envs: Environment variable names and values for the container command.
            index: The index of the container to execute the command in (default 1) if there are multiple containers for this service.
            tty: If `True`, allocate a pseudo-TTY. Use `False` to get the output of the command.
            privileged: If `True`, run the command in privileged mode.
            user: The username to use inside the container.
            workdir: The working directory inside the container.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Applies to detached, TTY and captured calls.

        Returns:
            Captured stdout when `detach=False` and `tty=False`; otherwise `None`.
            With the default `tty=True`, output goes to the caller's terminal.

        Raises:
            ValueError: Timeout is boolean, nonfinite, zero or negative.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped, without guaranteeing container-command termination.
            DockerException: The local Compose CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "exec"])
        full_cmd.add_flag("--detach", detach)
        env_arguments, child_env = container_environment(envs, self.client_config)
        full_cmd.add_args_iterable("--env", env_arguments)
        full_cmd.add_simple_arg("--index", index)
        full_cmd.add_flag("--no-tty", not tty)
        full_cmd.add_flag("--privileged", privileged)
        full_cmd.add_simple_arg("--user", user)
        full_cmd.add_simple_arg("--workdir", workdir)
        full_cmd += [service, *command]
        if detach:
            run(full_cmd, env=child_env, timeout=timeout)
        elif tty:
            run(
                full_cmd,
                env=child_env,
                capture_stdout=False,
                capture_stderr=False,
                timeout=timeout,
                workload_status=True,
            )
        else:
            return run(full_cmd, env=child_env, timeout=timeout, workload_status=True)

    def kill(
        self,
        services: str | list[str] | None = None,
        signal: int | str | None = None,
        *,
        remove_orphans: bool = False,
    ) -> None:
        """Send a signal to the containers of selected services.

        Args:
            services: One or more service(s) to kill. The default (`None`) is to kill all services.
                A string means the call will kill one single service. A list of service names can
                be provided to kill multiple services in one function call.
                An empty list means that no services are going to be killed, the function is then
                a no-op.
            signal: the signal to send to the container. Default is `"SIGKILL"`
            remove_orphans: Remove containers for services not defined in the
                Compose file.
        """
        full_cmd = Command([*self.docker_compose_cmd, "kill"])
        full_cmd.add_simple_arg("--signal", format_signal_arg(signal))
        full_cmd.add_flag("--remove-orphans", remove_orphans)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        run(full_cmd)

    @overload
    def logs(
        self,
        services: str | Sequence[str] = (),
        tail: str | None = None,
        follow: bool = False,
        no_log_prefix: bool = False,
        timestamps: bool = False,
        since: str | None = None,
        until: str | None = None,
        stream: Literal[False] = False,
        *,
        index: int | None = None,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def logs(
        self,
        services: str | Sequence[str] = (),
        tail: str | None = None,
        follow: bool = False,
        no_log_prefix: bool = False,
        timestamps: bool = False,
        since: str | None = None,
        until: str | None = None,
        *,
        stream: Literal[True],
        index: int | None = None,
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def logs(
        self,
        services: str | Sequence[str] = (),
        tail: str | None = None,
        follow: bool = False,
        no_log_prefix: bool = False,
        timestamps: bool = False,
        since: str | None = None,
        until: str | None = None,
        stream: bool = False,
        *,
        index: int | None = None,
        timeout: float | None = None,
    ) -> str | ProcessStream: ...

    def logs(
        self,
        services: str | Sequence[str] = (),
        tail: str | None = None,
        follow: bool = False,
        no_log_prefix: bool = False,
        timestamps: bool = False,
        since: str | None = None,
        until: str | None = None,
        stream: bool = False,
        *,
        index: int | None = None,
        timeout: float | None = None,
    ) -> str | ProcessStream:
        """Collect or stream output from service containers.

        The local logs CLI starts during the call. Blocking collection joins
        decoded stdout and stderr records in arrival order. When streaming,
        exhaust the owned stream or close it on early exit; a bare loop break
        does not close it. Closing affects the local CLI, not running services.

        Args:
            services: Services to view. An empty sequence selects all services,
                unlike commands where `services=[]` is a no-op.
            tail: Number of lines to show from the end of the logs for each container. (default "all")
            follow: Keep following new output. Blocking collection waits for the
                CLI to end, which may be indefinite; use streaming to consume
                records as they arrive.
            no_log_prefix: Don't print prefix in logs
            timestamps: Show timestamps
            since: Show logs since timestamp (e.g. 2013-01-02T13:23:37Z) or relative (e.g. 42m for 42 minutes)
            until: Show logs before a timestamp (e.g. 2013-01-02T13:23:37Z) or relative (e.g. 42m for 42 minutes)
            stream: Return an owned stream of `("stdout", bytes)` and
                `("stderr", bytes)` records instead of collecting text.
            index: Replica index when a service has several containers; `None`
                shows every replica.
            timeout: Positive finite seconds from local logs CLI launch, or `None`
                to inherit the client default. Timing continues while consumption pauses.
                Expiry raises `DockerTimeout` without stopping services or changing
                post-exit pipe draining.

        Returns:
            Collected log text when `stream=False`, or an owned stream of
            `(source, bytes)` records when `stream=True`.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local logs CLI exceeds its deadline and is killed
                and reaped.
            DockerException: The local logs CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "logs", "--no-color"])
        full_cmd.add_simple_arg("--tail", tail)
        full_cmd.add_flag("--follow", follow)
        full_cmd.add_flag("--no-log-prefix", no_log_prefix)
        full_cmd.add_flag("--timestamps", timestamps)
        full_cmd.add_simple_arg("--since", since)
        full_cmd.add_simple_arg("--until", until)
        full_cmd.add_simple_arg("--index", index)
        full_cmd += to_list(services)

        iterator = stream_stdout_and_stderr(full_cmd, timeout=timeout)
        if stream:
            return iterator
        else:
            with iterator:
                return "".join(x[1].decode() for x in iterator)

    def pause(self, services: str | list[str] | None = None) -> None:
        """Pause one or more services.

        Args:
            services: `None` (the default) means pause all containers of all
                compose services. A string means that the call will pause the container
                of a specific service. A list of string means the call will pause
                the containers of all the services specified. So if an empty list
                is provided, then this function call is a no-op.
        """
        full_cmd = Command([*self.docker_compose_cmd, "pause"])
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        run(full_cmd)

    def port(
        self,
        service: str,
        private_port: str | int,
        index: int = 1,
        protocol: str = "tcp",
    ) -> tuple[str | None, int | None]:
        """Return the published host and port for a service port binding.

        Args:
            service: The name of the service.
            private_port: The private port.
            index: Index of the container if service has multiple replicas (default 1)
            protocol: tcp or udp (default "tcp").

        Returns:
            The `(host, port)` pair, or `(None, None)` when the CLI reports `:0`.

        Raises:
            ValueError: `service` or `private_port` is an empty string.
        """
        full_cmd = Command([*self.docker_compose_cmd, "port"])
        if service == "":
            raise ValueError("Service cannot be empty")
        if private_port == "":
            raise ValueError("Private port cannot be empty")

        full_cmd.add_simple_arg("--index", index)
        full_cmd.add_simple_arg("--protocol", protocol)
        full_cmd += [service, private_port]

        result = run(full_cmd)
        if result == ":0":
            # docker compose cli joins host:str with port:int in the result. If port is unknown
            # then result has default value for the both variables (str->empty string, int -> 0)
            return None, None

        host, port = str(result).split(":")
        return host, int(port)

    @overload
    def ps(
        self,
        services: list[str] | None = ...,
        all: bool = ...,
        *,
        filters: Mapping[str, str] | Iterable[tuple[str, str]] = ...,
        status: ComposePsStatus | Sequence[ComposePsStatus] = ...,
        orphans: bool | None = ...,
        service_names: Literal[False] = ...,
    ) -> list[pywhales.components.container.cli_wrapper.Container]: ...

    @overload
    def ps(
        self,
        services: list[str] | None = ...,
        all: bool = ...,
        *,
        filters: Mapping[str, str] | Iterable[tuple[str, str]] = ...,
        status: ComposePsStatus | Sequence[ComposePsStatus] = ...,
        orphans: bool | None = ...,
        service_names: Literal[True],
    ) -> list[str]: ...

    @overload
    def ps(
        self,
        services: list[str] | None = ...,
        all: bool = ...,
        *,
        filters: Mapping[str, str] | Iterable[tuple[str, str]] = ...,
        status: ComposePsStatus | Sequence[ComposePsStatus] = ...,
        orphans: bool | None = ...,
        service_names: bool = ...,
    ) -> list[pywhales.components.container.cli_wrapper.Container] | list[str]: ...

    def ps(
        self,
        services: list[str] | None = None,
        all: bool = False,
        *,
        filters: Mapping[str, str] | Iterable[tuple[str, str]] = MappingProxyType({}),
        status: ComposePsStatus | Sequence[ComposePsStatus] = (),
        orphans: bool | None = None,
        service_names: bool = False,
    ) -> list[pywhales.components.container.cli_wrapper.Container] | list[str]:
        """Return containers created by the current Compose project, or its services.

        Args:
            services: Limit results to these services. `None` and `[]` both
                select all services, rather than skipping execution.
            all: Include stopped containers.
            filters: At most one filter, as a mapping or `(key, value)` pairs.
                The CLI supports only `status`, such as `{"status": "running"}`;
                it keeps just the last `--filter`, so more than one is rejected.
            status: One status, or several, to keep. Each is passed as its own
                `--status`. Known values are `paused`, `restarting`, `removing`,
                `running`, `dead`, `created` and `exited`.
            orphans: Include orphaned services, which the project doesn't declare.
                `None` leaves the CLI default (included); `False` passes
                `--orphans=false`.
            service_names: Return service names instead of container handles.

        Returns:
            Container handles in CLI output order, retaining the originating
            client configuration, or with `service_names=True` the service names
            in CLI order.

        Raises:
            ValueError: Dry-run output cannot be interpreted as container IDs or
                service names, or more than one filter is given. Raised before
                the CLI is resolved.
            TypeError: `filters` is a string, bytes, or an iterable containing
                anything but `(key, value)` tuples of strings.
        """
        if self.client_config.compose_dry_run:
            raise ValueError(
                "compose.ps() cannot return containers or services in dry-run mode."
            )
        filter_args = format_filters_for_cli(filters)
        if len(filter_args) > 1:
            raise ValueError("compose ps accepts at most one filter")
        full_cmd = Command(
            [
                *self.docker_compose_cmd,
                "ps",
                "--services" if service_names else "--quiet",
            ]
        )
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable("--filter", filter_args)
        full_cmd.add_args_iterable_or_single("--status", status)
        _add_optional_bool(full_cmd, "--orphans", orphans)
        if services is not None:
            full_cmd += services
        result = run(full_cmd)
        if service_names:
            return result.splitlines()
        ids = result.splitlines()
        # The first line might be a warning for experimental
        # See https://github.com/docker/compose-cli/issues/1108
        if len(ids) > 0 and "experimental" in ids[0]:
            ids.pop(0)

        Container = pywhales.components.container.cli_wrapper.Container
        return [Container(self.client_config, x, is_immutable_id=True) for x in ids]

    def ls(
        self,
        all: bool = False,
        filters: Mapping[str, str] | Iterable[ComposeLsFilter] = MappingProxyType({}),
    ) -> list[ComposeProject]:
        """Return Docker Compose projects reported by the CLI.

        Args:
            all: Results include all stopped compose projects.
            filters: Filter results based on conditions provided, either as a
                mapping or as an iterable of ``(key, value)`` pairs. Pairs keep
                their order and repeated keys, and an iterator is read once.
                The key is `name`. For example, `filters={"name": "web"}` or
                `filters=[("name", "web"), ("name", "worker")]`.

        Returns:
            Project summaries in CLI order, including parsed status counts and
            configuration-file paths when available.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
            ParsingError: If the output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        filter_args = format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_compose_cmd, "ls", "--format", "json"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable_or_single("--filter", filter_args)

        parser = JsonOutputParser(
            "compose project list", self.client_config.raw_diagnostics
        )
        return parser.parse(_parse_projects, run(full_cmd))

    def publish(
        self,
        repository: str,
        oci_version: str | None = None,
        resolve_image_digests: bool = False,
        with_env: bool = False,
        allow_publishing_sensitive_data: bool = False,
        *,
        app: bool = False,
    ) -> None:
        """Publish compose application.

        Args:
            repository: Repository to publish compose application
            oci_version: OCI image/artifact specification version (automatically determined by default)
            resolve_image_digests: Pin image tags to digests
            with_env: Include environment variables in the published OCI artifact
            allow_publishing_sensitive_data: Assume "yes" as answer to all prompts
            app: Publish the application, including the images it references.
        """
        full_cmd = Command([*self.docker_compose_cmd, "publish"])
        full_cmd.add_flag("--app", app)
        if oci_version is not None:
            full_cmd.add_simple_arg("--oci-version", oci_version)
        full_cmd.add_flag("--resolve-image-digests", resolve_image_digests)
        full_cmd.add_flag("--with-env", with_env)
        full_cmd.add_flag("--yes", allow_publishing_sensitive_data)
        full_cmd.append(repository)
        run(full_cmd, capture_stderr=False, capture_stdout=False)

    @overload
    def pull(
        self,
        services: list[str] | str | None = ...,
        ignore_pull_failures: bool = ...,
        include_deps: bool = ...,
        quiet: bool = ...,
        *,
        stream_logs: Literal[True],
        ignore_buildable: bool = ...,
        policy: ComposePullPolicyInput | None = ...,
        timeout: float | None = ...,
    ) -> ProcessStream | None: ...

    @overload
    def pull(
        self,
        services: list[str] | str | None = ...,
        ignore_pull_failures: bool = ...,
        include_deps: bool = ...,
        quiet: bool = ...,
        stream_logs: Literal[False] = ...,
        *,
        ignore_buildable: bool = ...,
        policy: ComposePullPolicyInput | None = ...,
        timeout: float | None = ...,
    ) -> None: ...

    @overload
    def pull(
        self,
        services: list[str] | str | None = None,
        ignore_pull_failures: bool = False,
        include_deps: bool = False,
        quiet: bool = False,
        stream_logs: bool = False,
        *,
        ignore_buildable: bool = False,
        policy: ComposePullPolicyInput | None = None,
        timeout: float | None = None,
    ) -> ProcessStream | None: ...

    def pull(
        self,
        services: list[str] | str | None = None,
        ignore_pull_failures: bool = False,
        include_deps: bool = False,
        quiet: bool = False,
        stream_logs: bool = False,
        *,
        ignore_buildable: bool = False,
        policy: ComposePullPolicyInput | None = None,
        timeout: float | None = None,
    ) -> ProcessStream | None:
        """Pull images for selected Compose services.

        Args:
            services: The list of services to select. Only the images of those
                services will be pulled. If no services are specified (`None`) (the default
                behavior) all images of all services are pulled.
                If an empty list is provided, then the function call is a no-op.
            ignore_pull_failures: Pull what it can and ignores images with pull failures
            include_deps: Also pull services declared as dependencies
            quiet: By default, the progress bars are printed in stdout and stderr (both).
                To disable all output, use `quiet=True`
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.
            ignore_buildable: Skip images that can be built.
            policy: Apply a pull policy, `"missing"` or `"always"`; `None`
                leaves the CLI default.
            timeout: Positive finite seconds from local CLI launch, replacing the
                client default; `None` inherits it. Booleans are rejected before
                selecting services, even for an empty request.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`, or
                `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped, without undoing pulled layers or guaranteeing that
                daemon-side transfers stop.
        """
        validate_timeout(timeout)
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )
        full_cmd = Command([*self.docker_compose_cmd, "pull"])
        full_cmd.add_flag("--ignore-pull-failures", ignore_pull_failures)
        full_cmd.add_flag("--include-deps", include_deps)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.add_flag("--ignore-buildable", ignore_buildable)
        full_cmd.add_simple_arg("--policy", policy)
        if services == []:
            return
        elif services is not None:
            services = to_list(services)
            full_cmd += services
        if stream_logs:
            return stream_stdout_and_stderr(full_cmd, timeout=timeout)
        else:
            run(full_cmd, capture_stdout=False, capture_stderr=False, timeout=timeout)

    def push(
        self,
        services: list[str] | None = None,
        *,
        ignore_missing: bool = False,
        include_deps: bool = False,
        quiet: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Push service images.

        Args:
            services: The list of services to select. Only the images of those
                services will be pushed. If no services are specified (`None`, the default
                behavior) all images of all services are pushed.
                If an empty list is provided, then the function call is a no-op.
            ignore_missing: Skip images that aren't available locally instead of
                failing to push them.
            include_deps: Also push the images of services declared as
                dependencies.
            quiet: Push without printing progress information.
            timeout: Positive finite seconds from local CLI launch, replacing the
                client default; `None` inherits it. Validated before selecting
                services, even for an empty request.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped, without undoing pushed layers or guaranteeing that
                registry or daemon-side work stops.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "push"])
        full_cmd.add_flag("--ignore-missing", ignore_missing)
        full_cmd.add_flag("--include-deps", include_deps)
        full_cmd.add_flag("--quiet", quiet)
        if services == []:
            return
        elif services is not None:
            full_cmd += services
        run(full_cmd, timeout=timeout)

    def restart(
        self,
        services: str | list[str] | None = None,
        timeout: int | timedelta | None = None,
        *,
        no_deps: bool = False,
    ) -> None:
        """Restart containers for selected services.

        Args:
            services: Names of one or more services to restart.
                If the argument is not specified, `services` is `None` and all services are restarted.
                If `services` is an empty list, then the function call is a no-op.
            timeout: Container shutdown grace period in seconds, or a duration
                truncated to whole seconds. `None` leaves the CLI default unchanged;
                this is not a local subprocess deadline.
            no_deps: Don't restart the services the selection depends on.
        """
        full_cmd = Command([*self.docker_compose_cmd, "restart"])
        full_cmd.add_flag("--no-deps", no_deps)

        if isinstance(timeout, timedelta):
            timeout = int(timeout.total_seconds())

        full_cmd.add_simple_arg("--timeout", timeout)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        run(full_cmd)

    def rm(
        self,
        services: str | list[str] | None = None,
        stop: bool = False,
        volumes: bool = False,
    ) -> None:
        """Remove stopped service containers.

        By default, anonymous volumes attached to containers will not be removed. You
        can override this with `volumes=True`.

        Any data which is not in a volume will be lost.

        Args:
            services: Names of one or more services whose containers to remove.
                If `None` (the default) then all services are removed.
                If an empty list is provided, this function call is a no-op.
            stop: Stop the containers, if required, before removing
            volumes: Remove any anonymous volumes attached to containers
        """
        full_cmd = Command([*self.docker_compose_cmd, "rm", "--force"])
        full_cmd.add_flag("--stop", stop)
        full_cmd.add_flag("--volumes", volumes)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        run(full_cmd)

    @overload
    def run(
        self,
        service: str,
        command: Sequence[str] = (),
        *,
        detach: Literal[True],
        stream: bool = False,
        **kwargs: Unpack[ComposeRunArgs],
    ) -> pywhales.components.container.cli_wrapper.Container: ...

    @overload
    def run(
        self,
        service: str,
        command: Sequence[str] = (),
        *,
        detach: Literal[False] = False,
        stream: Literal[True],
        **kwargs: Unpack[ComposeRunArgs],
    ) -> ProcessStream: ...

    @overload
    def run(
        self,
        service: str,
        command: Sequence[str] = (),
        *,
        detach: Literal[False] = False,
        stream: Literal[False] = False,
        **kwargs: Unpack[ComposeRunArgs],
    ) -> str: ...

    @overload
    def run(
        self,
        service: str,
        command: Sequence[str] = (),
        build: bool = False,
        detach: bool = False,
        entrypoint: str | None = None,
        envs: Mapping[str, str] = MappingProxyType({}),
        labels: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        tty: bool = True,
        stream: bool = False,
        dependencies: bool = True,
        publish: Sequence[
            pywhales.components.container.cli_wrapper.ValidPortMapping
        ] = (),
        remove: bool = False,
        service_ports: bool = False,
        use_aliases: bool = False,
        user: str | None = None,
        volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition] = (),
        workdir: str | Path | None = None,
        *,
        cap_add: Iterable[str] = (),
        cap_drop: Iterable[str] = (),
        env_from_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool | None = None,
        no_recreate: bool = False,
        pull: ComposeUpPullPolicy | None = None,
        quiet: bool = False,
        quiet_build: bool = False,
        quiet_pull: bool = False,
        remove_orphans: bool = False,
        timeout: float | None = None,
    ) -> str | pywhales.components.container.cli_wrapper.Container | ProcessStream: ...

    def run(
        self,
        service: str,
        command: Sequence[str] = (),
        build: bool = False,
        detach: bool = False,
        entrypoint: str | None = None,
        envs: Mapping[str, str] = MappingProxyType({}),
        labels: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        tty: bool = True,
        stream: bool = False,
        dependencies: bool = True,
        publish: Sequence[
            pywhales.components.container.cli_wrapper.ValidPortMapping
        ] = (),
        remove: bool = False,
        service_ports: bool = False,
        use_aliases: bool = False,
        user: str | None = None,
        volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition] = (),
        workdir: str | Path | None = None,
        *,
        cap_add: Iterable[str] = (),
        cap_drop: Iterable[str] = (),
        env_from_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool | None = None,
        no_recreate: bool = False,
        pull: ComposeUpPullPolicy | None = None,
        quiet: bool = False,
        quiet_build: bool = False,
        quiet_pull: bool = False,
        remove_orphans: bool = False,
        timeout: float | None = None,
    ) -> str | pywhales.components.container.cli_wrapper.Container | ProcessStream:
        """Run a one-off command on a service.

        The CLI starts during the call. With `stream=True`, close the owned
        stream on early exit or use its context manager. Closing or timing out
        the local CLI does not guarantee that the container command stops.

        Args:
            service: The name of the service.
            command: Argument sequence overriding the service command; the empty
                default keeps the service command.
            build: Build the service image before running.
            detach: Return a container handle after the detached CLI completes.
                Requires `tty=False` and cannot be combined with streaming or
                `compose_dry_run=True`.
            entrypoint: The entrypoint to execute.
            envs: Environment variable names and values for the container command.
            labels: Add or override labels
            name: Assign a name to the container.
            tty: Allocate a pseudo-TTY and forward output to the caller's terminal.
                Defaults to `True`; set `False` for streaming or detached runs.
            stream: Return an owned stream of `("stdout", bytes)` and
                `("stderr", bytes)` records. Requires `tty=False` and `detach=False`.
            dependencies: Also start linked services.
            publish: Publish a container's port(s) to the host.
            remove: Automatically remove the container when it exits.
            service_ports: Enable service's ports and map them to the host.
            use_aliases: Use the service's network aliases in the connected network(s).
            user: Username or UID, format: `"<name|uid>[:<group|gid>]"`
            volumes: Mount definitions containing a source, container destination,
                and optional mode. Bind sources refer to the daemon host, which
                may differ from the Python host; avoid exposing sensitive paths.
            workdir: Working directory inside the container
            cap_add: Linux capabilities to add, one `--cap-add` each.
            cap_drop: Linux capabilities to drop, one `--cap-drop` each.
            env_from_files: Environment files read by the CLI, one
                `--env-from-file` each.
            interactive: Keep stdin open even if not attached. `None` leaves the
                CLI default (open); `False` passes `--interactive=false`.
            no_recreate: Don't recreate dependency containers that already exist,
                even if their configuration diverged.
            pull: Image acquisition policy, `"always"`, `"missing"` or
                `"never"`; `None` leaves the Compose default.
            quiet: Don't print anything to stdout.
            quiet_build: Suppress build progress output.
            quiet_pull: Pull without printing progress information.
            remove_orphans: Remove containers for services not defined in the
                Compose file.
            timeout: Positive finite seconds from local CLI launch, or `None`
                to inherit the client default. Timing advances even when a returned stream is
                not consumed. This is not a Compose shutdown or health-wait timeout.

        Returns:
            A container handle retaining the originating configuration when
            detached, an owned byte-record stream when streaming, or captured
            stdout otherwise. With the default `tty=True`, output is forwarded
            to the terminal and the returned string is empty. Attached dry-run
            calls with `tty=False` can return preview text.

        Raises:
            ValueError: Streaming conflicts with TTY or detach, detach conflicts
                with TTY or dry-run mode, or `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped, without guaranteeing container termination.
            DockerException: The local Compose CLI exits unsuccessfully.
        """

        validate_timeout(timeout)
        if detach and self.client_config.compose_dry_run:
            raise ValueError(
                "Detached compose.run() cannot return a container in dry-run mode."
            )

        if tty and stream:
            raise ValueError(
                "You can't set tty=True and stream=True at the same"
                "time. Their purpose are not compatible. Try setting tty=False in docker.compose.run"
            )

        if detach and stream:
            raise ValueError(
                "You can't detach and stream at the same time. It's not compatible."
            )

        if detach and tty:
            raise ValueError(
                "You can't detach and set tty=True at the same time. It's not compatible. "
                "Try setting tty=False in docker.compose.run(...)."
            )
        full_cmd = Command([*self.docker_compose_cmd, "run"])
        full_cmd.add_flag("--build", build)
        full_cmd.add_flag("--detach", detach)
        full_cmd.add_simple_arg("--entrypoint", entrypoint)
        env_arguments, child_env = container_environment(envs, self.client_config)
        full_cmd.add_args_iterable("--env", env_arguments)
        full_cmd.add_simple_arg("--name", name)
        full_cmd.add_flag("--no-tty", not tty)
        full_cmd.add_flag("--no-deps", not dependencies)
        for port_mapping in publish:
            full_cmd += ["--publish", format_port_arg(port_mapping)]

        full_cmd.add_flag("--rm", remove)
        full_cmd.add_flag("--service-ports", service_ports)
        full_cmd.add_flag("--use-aliases", use_aliases)
        full_cmd.add_simple_arg("--user", user)
        for volume_definition in volumes:
            volume_definition = tuple(str(x) for x in volume_definition)
            full_cmd += ["--volume", ":".join(volume_definition)]
        full_cmd.add_simple_arg("--workdir", workdir)
        full_cmd.add_args_iterable_or_single("--label", format_mapping_for_cli(labels))
        full_cmd.add_args_iterable("--cap-add", cap_add)
        full_cmd.add_args_iterable("--cap-drop", cap_drop)
        full_cmd.add_args_iterable_or_single("--env-from-file", env_from_files)
        _add_optional_bool(full_cmd, "--interactive", interactive)
        full_cmd.add_flag("--no-recreate", no_recreate)
        full_cmd.add_simple_arg("--pull", pull)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.add_flag("--quiet-build", quiet_build)
        full_cmd.add_flag("--quiet-pull", quiet_pull)
        full_cmd.add_flag("--remove-orphans", remove_orphans)
        full_cmd.append(service)
        full_cmd += command

        if stream:
            return stream_stdout_and_stderr(
                full_cmd, env=child_env, timeout=timeout, workload_status=True
            )
        else:
            result = run(
                full_cmd,
                env=child_env,
                tty=tty,
                timeout=timeout,
                workload_status=not detach,
            )
            if detach:
                Container = pywhales.components.container.cli_wrapper.Container
                return Container(self.client_config, result, is_immutable_id=True)
            else:
                return result

    @overload
    def start(
        self,
        services: str | list[str] | None = ...,
        *,
        stream_logs: Literal[True],
        wait: bool = ...,
        wait_timeout: int | None = ...,
    ) -> ProcessStream | None: ...

    @overload
    def start(
        self,
        services: str | list[str] | None = ...,
        stream_logs: Literal[False] = ...,
        *,
        wait: bool = ...,
        wait_timeout: int | None = ...,
    ) -> None: ...

    @overload
    def start(
        self,
        services: str | list[str] | None = None,
        stream_logs: bool = False,
        *,
        wait: bool = False,
        wait_timeout: int | None = None,
    ) -> ProcessStream | None: ...

    def start(
        self,
        services: str | list[str] | None = None,
        stream_logs: bool = False,
        *,
        wait: bool = False,
        wait_timeout: int | None = None,
    ) -> ProcessStream | None:
        """Start the specified services.

        Args:
            services: The names of one or more services to start.
                If `None` (the default), it means all services will start.
                If an empty list is provided, this function call is a no-op.
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.
            wait: Have Compose wait until the services are running or healthy.
                Implies detached mode.
            wait_timeout: Maximum seconds for Compose to wait; `None` omits the
                flag. This is not a local subprocess deadline.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.
        """
        full_cmd = Command([*self.docker_compose_cmd, "start"])
        full_cmd.add_flag("--wait", wait)
        full_cmd.add_simple_arg("--wait-timeout", wait_timeout)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        if stream_logs:
            return stream_stdout_and_stderr(full_cmd)
        else:
            run(full_cmd)

    @overload
    def stop(
        self,
        services: str | list[str] | None = ...,
        timeout: int | timedelta | None = ...,
        *,
        stream_logs: Literal[True],
    ) -> ProcessStream | None: ...

    @overload
    def stop(
        self,
        services: str | list[str] | None = ...,
        timeout: int | timedelta | None = ...,
        stream_logs: Literal[False] = ...,
    ) -> None: ...

    @overload
    def stop(
        self,
        services: str | list[str] | None = None,
        timeout: int | timedelta | None = None,
        stream_logs: bool = False,
    ) -> ProcessStream | None: ...

    def stop(
        self,
        services: str | list[str] | None = None,
        timeout: int | timedelta | None = None,
        stream_logs: bool = False,
    ) -> ProcessStream | None:
        """Stop services.

        Args:
            services: Names of one or more services to stop.
                If `None` (the default), it means all services will stop.
                If an empty list is provided, this function call is a no-op.
            timeout: Container shutdown grace period, in seconds or as a timedelta
                truncated to whole seconds. `None` leaves the CLI default unchanged;
                this is not a local subprocess deadline.
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.
        """
        if isinstance(timeout, timedelta):
            timeout = int(timeout.total_seconds())

        full_cmd = Command([*self.docker_compose_cmd, "stop"])
        full_cmd.add_simple_arg("--timeout", timeout)
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        if stream_logs:
            return stream_stdout_and_stderr(full_cmd)
        else:
            run(full_cmd)

    @overload
    def top(
        self, services: str | list[str] | None = None, *, raw: Literal[False] = False
    ) -> ComposeTopResult: ...

    @overload
    def top(
        self, services: str | list[str] | None = None, *, raw: Literal[True]
    ) -> str: ...

    @overload
    def top(
        self, services: str | list[str] | None = None, *, raw: bool
    ) -> ComposeTopResult | str: ...

    def top(
        self, services: str | list[str] | None = None, *, raw: bool = False
    ) -> ComposeTopResult | str:
        """List processes using this client's Compose project configuration.

        Args:
            services: Service name(s), or None for all services. An explicit []
                returns an empty result without resolving or invoking the CLI.
            raw: Return CLI text without parsing, including unsupported layouts.

        Returns:
            Unmodified CLI text when `raw=True`, or a result retaining raw output
            and ordered sections otherwise. Grouped sections retain their
            container name; flat SERVICE/# tables retain attribution in their
            headings and cells, with `container=None`. For `services=[]`, return
            an empty string in raw mode or an empty parsed result otherwise.

        Raises:
            TopParseError: A malformed or ambiguous table; use raw=True.
            DockerException: CLI failures, including unknown services.
        """
        if services == []:
            return "" if raw else ComposeTopResult("", ())
        selected = [] if services is None else to_list(services)
        output = run([*self.docker_compose_cmd, "top", *selected])
        return output if raw else parse_compose_top(output)

    def events(
        self,
        services: Sequence[str] = (),
        *,
        since: str | None = None,
        until: str | None = None,
        timeout: float | None = None,
    ) -> MappedProcessStream[ComposeEvent]:
        """Return Docker Compose events for the specified services.

        The local events CLI starts before this call returns. Iteration can block
        waiting for the next event and continues until the CLI ends or is closed.
        Use the returned stream as a context manager or call `close()` when
        stopping early. A bare loop break does not release the local CLI; closing
        it does not stop services.

        Args:
            services: Service names to filter events. Empty input selects all services.
            since: Show events created since this timestamp.
            until: Stream events until this timestamp.
            timeout: Positive finite seconds from local events CLI launch, or
                `None` to inherit the client default. Timing advances while consumption pauses.
                Expiry raises `DockerTimeout` without stopping services or changing
                post-exit pipe draining.

        Returns:
            An owned iterator parsing stdout JSON records as Compose events.
            Stderr records are consumed but do not produce events.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local events CLI exceeds its deadline and is
                killed and reaped.
            DockerException: The local events CLI exits unsuccessfully.
            ParsingError: Raised once, during iteration, by the first malformed
                or unexpected stdout record, after the CLI is closed and reaped.
                The client's `raw_diagnostics` flag is read at launch: safe mode
                withholds the payload and chains no cause, raw mode chains the
                original error.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "events", "--json"])
        full_cmd.add_simple_arg("--since", since)
        full_cmd.add_simple_arg("--until", until)
        full_cmd.extend(services)

        parser = JsonOutputParser("compose event", self.client_config.raw_diagnostics)
        stream = stream_stdout_and_stderr(full_cmd, timeout=timeout)
        return MappedProcessStream(
            stream,
            (
                parser.parse(_parse_event, content)
                for origin, content in stream
                if origin == "stdout"
            ),
        )

    def unpause(self, services: str | list[str] | None = None) -> None:
        """Unpause one or more services.

        Args:
            services: One or more service to unpause.
                If `None` (the default), all services are unpaused.
                If services is an empty list, the function call does nothing,
                it's a no-op.
        """
        full_cmd = Command([*self.docker_compose_cmd, "unpause"])
        if services == []:
            return
        elif services is not None:
            full_cmd += to_list(services)
        run(full_cmd)

    @overload
    def up(
        self,
        services: list[str] | str | None = ...,
        build: bool = ...,
        detach: bool = ...,
        abort_on_container_exit: bool = ...,
        scales: Mapping[str, int] = ...,
        attach_dependencies: bool = ...,
        force_recreate: bool = ...,
        recreate: bool = ...,
        no_build: bool = ...,
        remove_orphans: bool = ...,
        renew_anon_volumes: bool = ...,
        color: bool = ...,
        log_prefix: bool = ...,
        start: bool = ...,
        quiet: bool = ...,
        wait: bool = ...,
        no_attach_services: list[str] | str | None = ...,
        pull: ComposeUpPullPolicy | None = ...,
        *,
        stream_logs: Literal[True],
        wait_timeout: int | None = ...,
        dependencies: bool = True,
        use_default_sensitive_values: bool = ...,
        watch: bool = ...,
        abort_on_container_failure: bool = ...,
        exit_code_from: str | None = ...,
        always_recreate_deps: bool = ...,
        attach_services: list[str] | str | None = ...,
        menu: bool | None = ...,
        quiet_build: bool = ...,
        quiet_pull: bool = ...,
        shutdown_timeout: int | timedelta | None = ...,
        timestamps: bool = ...,
    ) -> ProcessStream | None: ...

    @overload
    def up(
        self,
        services: list[str] | str | None = ...,
        build: bool = ...,
        detach: bool = ...,
        abort_on_container_exit: bool = ...,
        scales: Mapping[str, int] = ...,
        attach_dependencies: bool = ...,
        force_recreate: bool = ...,
        recreate: bool = ...,
        no_build: bool = ...,
        remove_orphans: bool = ...,
        renew_anon_volumes: bool = ...,
        color: bool = ...,
        log_prefix: bool = ...,
        start: bool = ...,
        quiet: bool = ...,
        wait: bool = ...,
        no_attach_services: list[str] | str | None = ...,
        pull: ComposeUpPullPolicy | None = ...,
        stream_logs: Literal[False] = ...,
        wait_timeout: int | None = ...,
        dependencies: bool = True,
        use_default_sensitive_values: bool = ...,
        *,
        watch: bool = ...,
        abort_on_container_failure: bool = ...,
        exit_code_from: str | None = ...,
        always_recreate_deps: bool = ...,
        attach_services: list[str] | str | None = ...,
        menu: bool | None = ...,
        quiet_build: bool = ...,
        quiet_pull: bool = ...,
        shutdown_timeout: int | timedelta | None = ...,
        timestamps: bool = ...,
    ) -> None: ...

    @overload
    def up(
        self,
        services: list[str] | str | None = None,
        build: bool = False,
        detach: bool = False,
        abort_on_container_exit: bool = False,
        scales: Mapping[str, int] = MappingProxyType({}),
        attach_dependencies: bool = False,
        force_recreate: bool = False,
        recreate: bool = True,
        no_build: bool = False,
        remove_orphans: bool = False,
        renew_anon_volumes: bool = False,
        color: bool = True,
        log_prefix: bool = True,
        start: bool = True,
        quiet: bool = False,
        wait: bool = False,
        no_attach_services: list[str] | str | None = None,
        pull: ComposeUpPullPolicy | None = None,
        stream_logs: bool = False,
        wait_timeout: int | None = None,
        dependencies: bool = True,
        use_default_sensitive_values: bool = False,
        *,
        watch: bool = False,
        abort_on_container_failure: bool = False,
        exit_code_from: str | None = None,
        always_recreate_deps: bool = False,
        attach_services: list[str] | str | None = None,
        menu: bool | None = None,
        quiet_build: bool = False,
        quiet_pull: bool = False,
        shutdown_timeout: int | timedelta | None = None,
        timestamps: bool = False,
    ) -> ProcessStream | None: ...

    def up(
        self,
        services: list[str] | str | None = None,
        build: bool = False,
        detach: bool = False,
        abort_on_container_exit: bool = False,
        scales: Mapping[str, int] = MappingProxyType({}),
        attach_dependencies: bool = False,
        force_recreate: bool = False,
        recreate: bool = True,
        no_build: bool = False,
        remove_orphans: bool = False,
        renew_anon_volumes: bool = False,
        color: bool = True,
        log_prefix: bool = True,
        start: bool = True,
        quiet: bool = False,
        wait: bool = False,
        no_attach_services: list[str] | str | None = None,
        pull: ComposeUpPullPolicy | None = None,
        stream_logs: bool = False,
        wait_timeout: int | None = None,
        dependencies: bool = True,
        use_default_sensitive_values: bool = False,
        *,
        watch: bool = False,
        abort_on_container_failure: bool = False,
        exit_code_from: str | None = None,
        always_recreate_deps: bool = False,
        attach_services: list[str] | str | None = None,
        menu: bool | None = None,
        quiet_build: bool = False,
        quiet_pull: bool = False,
        shutdown_timeout: int | timedelta | None = None,
        timestamps: bool = False,
    ) -> ProcessStream | None:
        """Start the containers.

        Streaming starts the local CLI before returning and exposes its output,
        including attached container logs. Close the owned stream on early exit
        or use its context manager; closing is not a service cleanup operation.

        Args:
            services: The services to start. If `None` (default), all services are
                started. If an empty list is provided, the function call does nothing, it's
                a no-op.
            build: If `True`, build the docker images before starting the containers
                even if a docker image with this name already exists.
                If `False` (the default), build only the docker images that do not already
                exist.
            detach: Run containers in the background. Blocking calls still wait
                for the local CLI, not necessarily for container termination.
                Incompatible with `abort_on_container_exit=True`.
            abort_on_container_exit: If `True` stops all containers if any container was
                stopped. Incompatible with `detach=True`.
            scales: Scale SERVICE to NUM instances. Overrides
                the scale setting in the Compose file if present. For example:
                `scales={"my_service": 2, "my_other_service": 5}`.
            attach_dependencies: Attach to dependent containers.
            force_recreate: Recreate containers even if their configuration and image
                haven't changed.
            recreate: Recreate the containers if already exist.
                `recreate=False` and `force_recreate=True` are incompatible.
            no_build: Don't build an image, even if it's missing.
            remove_orphans: Remove containers for services not defined in the Compose file.
            renew_anon_volumes: Recreate anonymous volumes instead of retrieving
                data from the previous containers.
            color: If `False`, it will produce monochrome output.
            log_prefix: If `False`, will not display the prefix in the logs.
            start: Start the service after creating them.
            quiet: By default, some progress bars and logs are sent to stderr and stdout.
                Set `quiet=True` to avoid having any output.
            wait: Wait for services to be running|healthy. Implies detached mode.
            no_attach_services: The services not to attach to.
            pull: Image acquisition policy, such as `"always"`, `"missing"`, or
                `"never"`; `None` leaves the Compose default unchanged.
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.
            wait_timeout: Maximum seconds for Compose to wait for services to be
                running or healthy; `None` omits the flag. This is not a local
                subprocess deadline.
            dependencies: Also start linked services.
            use_default_sensitive_values: If `True` assume "yes" as answer to all prompts and run non-interactively.
            watch: Watch source changes and rebuild or refresh services. Requires
                a supporting Compose plugin; use `stream_logs=True` to consume output.
            abort_on_container_failure: Stop all containers if any exits with a
                failure. Incompatible with detached mode.
            exit_code_from: Return the selected service's exit code through the CLI;
                implies abort-on-container-exit. A nonzero exit raises `DockerException`.
            always_recreate_deps: Recreate dependent containers. Incompatible with
                `recreate=False`.
            attach_services: The only services whose logs are attached. Incompatible
                with `attach_dependencies=True`. `None` omits the option.
            menu: Enable interactive shortcuts when running attached. `None`
                leaves the CLI default and its `COMPOSE_MENU` setting; `False`
                passes `--menu=false`. Incompatible with `detach=True`.
            quiet_build: Suppress the build output.
            quiet_pull: Pull without printing progress information.
            shutdown_timeout: Seconds, or a duration truncated to whole seconds,
                for container shutdown when attached or when containers are
                already running. This is Compose's `--timeout`, not a local
                subprocess deadline; `None` omits it.
            timestamps: Show timestamps in the attached logs.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`
            and services are selected; otherwise `None`, including for `[]`.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`.
        """
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )
        if services == []:
            return
        full_cmd = Command([*self.docker_compose_cmd, "up"])
        full_cmd.add_flag("--build", build)
        full_cmd.add_flag("--detach", detach)
        full_cmd.add_flag("--wait", wait)
        full_cmd.add_simple_arg("--wait-timeout", wait_timeout)
        full_cmd.add_flag("--abort-on-container-exit", abort_on_container_exit)
        for service, scale in scales.items():
            full_cmd.add_simple_arg("--scale", f"{service}={scale}")
        full_cmd.add_flag("--attach-dependencies", attach_dependencies)
        full_cmd.add_flag("--force-recreate", force_recreate)
        full_cmd.add_flag("--no-recreate", not recreate)
        full_cmd.add_flag("--no-build", no_build)
        full_cmd.add_flag("--no-color", not color)
        full_cmd.add_flag("--no-log-prefix", not log_prefix)
        full_cmd.add_flag("--no-start", not start)
        full_cmd.add_flag("--remove-orphans", remove_orphans)
        full_cmd.add_flag("--renew-anon-volumes", renew_anon_volumes)
        full_cmd.add_flag("--no-deps", not dependencies)
        full_cmd.add_flag("--yes", use_default_sensitive_values)
        full_cmd.add_simple_arg("--pull", pull)
        full_cmd.add_flag("--watch", watch)
        full_cmd.add_flag("--abort-on-container-failure", abort_on_container_failure)
        full_cmd.add_simple_arg("--exit-code-from", exit_code_from)
        full_cmd.add_flag("--always-recreate-deps", always_recreate_deps)
        _add_optional_bool(full_cmd, "--menu", menu)
        full_cmd.add_flag("--quiet-build", quiet_build)
        full_cmd.add_flag("--quiet-pull", quiet_pull)
        full_cmd.add_simple_arg("--timeout", _whole_seconds(shutdown_timeout))
        full_cmd.add_flag("--timestamps", timestamps)
        if attach_services is not None:
            full_cmd.add_args_iterable("--attach", to_list(attach_services))

        if no_attach_services is not None:
            no_attach_services = to_list(no_attach_services)
            for service in no_attach_services:
                full_cmd.add_simple_arg("--no-attach", service)

        if services is not None:
            services = to_list(services)
            full_cmd += services

        if stream_logs:
            return stream_stdout_and_stderr(full_cmd)
        else:
            # important information is written to both stdout AND stderr.
            run(full_cmd, capture_stdout=quiet, capture_stderr=quiet)

    def watch(
        self,
        services: list[str] | str | None = None,
        *,
        no_up: bool = False,
        quiet: bool = False,
        prune: bool = False,
        timeout: float | None = None,
    ) -> ProcessStream | None:
        """Watch source changes and rebuild or refresh Compose services.

        A nonempty selection starts the local CLI before returning. Requires a
        supporting Compose plugin; no version probe is performed.

        Args:
            services: Select services, or `None` for all services. An empty list
                is a no-op and returns `None` without invoking the CLI.
            no_up: Do not build and start services before watching.
            quiet: Hide build output; other watch messages can still be streamed.
            prune: Prune dangling images on rebuild. Defaults to `False`, explicitly
                overriding the CLI's pruning default to avoid shared-image cleanup.
            timeout: Positive finite seconds from local CLI launch, replacing the
                client default; `None` inherits it. Timing advances while stream
                consumption pauses. Validated before selecting services, even
                for an empty request.

        Returns:
            `None` for `services=[]`; otherwise an owned stream of
            `("stdout", bytes)` and `("stderr", bytes)` records. Use it as a
            context manager or call `close()` when stopping early; a bare loop
            break does not release the local CLI. Closing reaps the CLI without
            removing running services.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerException: The local watch CLI exits unsuccessfully.
            DockerTimeout: The local watch CLI exceeds its deadline and is killed
                and reaped without stopping services.
        """
        validate_timeout(timeout)
        if services == []:
            return None
        full_cmd = Command([*self.docker_compose_cmd, "watch"])
        full_cmd.add_flag("--no-up", no_up)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.append(f"--prune={str(prune).lower()}")
        if services is not None:
            full_cmd += to_list(services)
        return stream_stdout_and_stderr(full_cmd, timeout=timeout)

    def version(self, *, short: bool = False) -> str:
        """Return the Docker Compose version text reported by the CLI.

        Args:
            short: Show only the version number, such as `5.7.1`.
        """
        full_cmd = Command([*self.docker_compose_cmd, "version"])
        full_cmd.add_flag("--short", short)
        return run(full_cmd)

    def commit(
        self,
        service: str,
        repository: str | None = None,
        *,
        author: str | None = None,
        changes: str | Iterable[str] = (),
        index: int | None = None,
        message: str | None = None,
        pause: bool = True,
        timeout: float | None = None,
    ) -> pywhales.components.image.cli_wrapper.Image:
        """Create an image from a service container's changes.

        Compose prints the new image ID only as progress text, so the command
        always runs with `--progress plain` and ignores `compose_progress`;
        quiet, tty and truncated modes would hide or shorten the ID.

        Args:
            service: The service whose container is committed.
            repository: Repository and optional tag for the image, such as
                `"app:v2"`; `None` leaves the image untagged.
            author: Author, such as `"Name <name@example.com>"`.
            changes: Dockerfile instructions to apply, such as `"ENV A=1"`; one
                `--change` is passed for each.
            index: Container index when the service has several replicas; `None`
                leaves the CLI default (the first).
            message: Commit message.
            pause: Pause the container during the commit. `False` passes
                `--pause=false`; `True` leaves the CLI default.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Returns:
            The committed image, retaining the originating client configuration.

        Raises:
            ValueError: `compose_dry_run` is set, because a preview creates no
                image, or `timeout` is not positive and finite.
            ParsingError: The output doesn't report a full image ID.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; a commit already in progress isn't guaranteed to stop.
            DockerException: The service isn't running or the CLI fails.
        """
        validate_timeout(timeout)
        if self.client_config.compose_dry_run:
            raise ValueError("compose.commit() cannot return an image in dry-run mode.")
        full_cmd = Command(
            [*self.client_config.get_compose_command(progress="plain"), "commit"]
        )
        full_cmd.add_simple_arg("--author", author)
        full_cmd.add_args_iterable_or_single("--change", changes)
        full_cmd.add_simple_arg("--index", index)
        full_cmd.add_simple_arg("--message", message)
        full_cmd.add_flag("--pause=false", not pause)
        full_cmd.append(service)
        if repository is not None:
            full_cmd.append(repository)
        _, progress = run(full_cmd, return_stderr=True, timeout=timeout)
        committed = _COMMITTED_IMAGE.search(progress)
        if committed is None:
            raise ParsingError("Unexpected compose commit output")
        return pywhales.components.image.cli_wrapper.Image(
            self.client_config, committed.group(1), is_immutable_id=True
        )

    def cp(
        self,
        source: ValidPath | ComposeServicePath,
        destination: ValidPath | ComposeServicePath,
        *,
        all: bool = False,
        archive: bool = False,
        follow_link: bool = False,
        index: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """Copy files or folders between a service container and the local filesystem.

        Byte iterators and the `-` stream form are not supported. The call waits
        for the local copy CLI.

        Args:
            source: Local source path on the Python host, or a
                `(service, path_in_container)` endpoint.
            destination: Local destination path on the Python host, or a
                `(service, path_in_container)` endpoint. Compose requires exactly
                one service endpoint.
            all: Include containers created by `run()`.
            archive: Copy all uid/gid information.
            follow_link: Always follow symbolic links in the source path.
            index: Container index when the service has several replicas; `None`
                leaves the CLI default (the first).
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; a partial copy isn't undone.
            DockerException: The CLI fails, for example when the container is gone.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "cp"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_flag("--archive", archive)
        full_cmd.add_flag("--follow-link", follow_link)
        full_cmd.add_simple_arg("--index", index)
        for endpoint in (source, destination):
            if isinstance(endpoint, tuple):
                full_cmd.append(f"{endpoint[0]}:{endpoint[1]}")
            else:
                full_cmd.append(str(endpoint))
        run(full_cmd, timeout=timeout)

    def export(
        self,
        service: str,
        output: ValidPath,
        *,
        index: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """Export a service container's filesystem to a local tar archive.

        Args:
            service: The service whose container is exported.
            output: The path of the tar archive on the Python host. Returning the
                bytes isn't supported.
            index: Container index when the service has several replicas; `None`
                leaves the CLI default (the first).
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; a partial archive can remain.
            DockerException: The CLI fails, for example when the service has no
                container.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "export"])
        full_cmd.add_simple_arg("--index", index)
        full_cmd.add_simple_arg("--output", output)
        full_cmd.append(service)
        run(full_cmd, timeout=timeout)

    def images(self, services: str | Sequence[str] | None = None) -> list[ComposeImage]:
        """List the images used by the project's created containers.

        Args:
            services: Services to list. `None` and an empty sequence select every
                service, as in `ps()`.

        Returns:
            One entry per container, in CLI order, so an image shared by two
            containers appears twice. Empty when no container exists.

        Raises:
            ParsingError: The output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command([*self.docker_compose_cmd, "images", "--format", "json"])
        if services is not None:
            full_cmd += to_list(services)
        parser = JsonOutputParser(
            "compose image list", self.client_config.raw_diagnostics
        )
        return parser.parse(_parse_images, run(full_cmd))

    def scale(
        self,
        scales: Mapping[str, int],
        *,
        no_deps: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Set the number of containers of one or more services.

        Args:
            scales: Replica counts by service, such as `{"web": 2}`. An empty
                mapping is a no-op that returns without resolving the CLI.
            no_deps: Don't start linked services.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the no-op return.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; containers already created or stopped stay that way.
            DockerException: The CLI fails, for example for an unknown service.
        """
        validate_timeout(timeout)
        if not scales:
            return
        full_cmd = Command([*self.docker_compose_cmd, "scale"])
        full_cmd.add_flag("--no-deps", no_deps)
        full_cmd += format_mapping_for_cli(scales)
        run(full_cmd, timeout=timeout)

    @overload
    def stats(
        self,
        service: str | None = ...,
        *,
        all: bool = ...,
        aggregate: bool = ...,
        all_projects: bool = ...,
        stream: Literal[False] = ...,
        timeout: float | None = ...,
    ) -> list[ComposeStats]: ...

    @overload
    def stats(
        self,
        service: str | None = ...,
        *,
        all: bool = ...,
        aggregate: bool = ...,
        all_projects: bool = ...,
        stream: Literal[True],
        timeout: float | None = ...,
    ) -> ProcessStream: ...

    @overload
    def stats(
        self,
        service: str | None = ...,
        *,
        all: bool = ...,
        aggregate: bool = ...,
        all_projects: bool = ...,
        stream: bool = ...,
        timeout: float | None = ...,
    ) -> list[ComposeStats] | ProcessStream: ...

    def stats(
        self,
        service: str | None = None,
        *,
        all: bool = False,
        aggregate: bool = False,
        all_projects: bool = False,
        stream: bool = False,
        timeout: float | None = None,
    ) -> list[ComposeStats] | ProcessStream:
        """Return resource-usage samples for the project's containers.

        By default the call takes one untruncated snapshot and waits for it.
        With `stream=True` the local CLI starts during the call and keeps
        sampling; close the returned stream, or use its context manager, on
        early exit. A bare loop break doesn't release it.

        Args:
            service: Limit the samples to one service; `None` selects all.
            all: Include containers that aren't running.
            aggregate: Report one row per project, summing its containers.
            all_projects: Report the containers of every project instead of the
                current one. Compose rejects this together with a project name.
            stream: Return the live table as an owned stream of
                `("stdout", bytes)` and `("stderr", bytes)` records instead of
                a parsed snapshot. Each frame redraws the screen, so records can
                hold terminal control bytes, and nothing is parsed.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. A stream's deadline advances while
                consumption pauses. Validated before the CLI is resolved.

        Returns:
            One `ComposeStats` per output line in snapshot mode, empty when there
            is nothing to report, or the owned record stream.

        Raises:
            ValueError: `timeout` is not positive and finite.
            ParsingError: A snapshot line is malformed or has an unexpected
                shape. Safe mode withholds the payload and chains no cause; a
                client with `raw_diagnostics=True` chains the original error.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped.
            DockerException: The CLI fails, for example for an unknown service.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "stats"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_flag("--aggregate", aggregate)
        full_cmd.add_flag("--all-projects", all_projects)
        if not stream:
            full_cmd += ["--no-stream", "--no-trunc", "--format", "json"]
        if service is not None:
            full_cmd.append(service)
        if stream:
            return stream_stdout_and_stderr(full_cmd, timeout=timeout)
        parser = JsonOutputParser("compose stats", self.client_config.raw_diagnostics)
        return parser.parse(
            lambda output: _parse_json_lines(ComposeStats, output),
            run(full_cmd, timeout=timeout),
        )

    def volumes(
        self, services: str | Sequence[str] | None = None
    ) -> list[ComposeVolume]:
        """List the volumes used by the project's created containers.

        Args:
            services: Services to list. `None` and an empty sequence select every
                service, as in `ps()`.

        Returns:
            One entry per volume, in CLI order. Empty when none is in use.

        Raises:
            ParsingError: A line is malformed or has an unexpected shape. Safe
                mode withholds the payload and chains no cause; a client with
                `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command([*self.docker_compose_cmd, "volumes", "--format", "json"])
        if services is not None:
            full_cmd += to_list(services)
        parser = JsonOutputParser(
            "compose volume list", self.client_config.raw_diagnostics
        )
        return parser.parse(
            lambda output: _parse_json_lines(ComposeVolume, output), run(full_cmd)
        )

    def wait(
        self,
        services: str | Sequence[str],
        *,
        down_project: bool = False,
        timeout: float | None = None,
    ) -> int:
        """Block until the containers of the selected services stop.

        Compose reports the stopped container's status as its own exit code and
        prints a summary on stdout, so a nonzero container status is returned,
        not raised, when stderr is empty. A failure that writes to stderr, such
        as an unknown service, raises. With several services the status is the
        one Compose reports.

        Args:
            services: One service, or several. Compose requires at least one.
            down_project: Drop the project when the first container stops.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. The wait is unbounded without it.
                Validated before the CLI is resolved.

        Returns:
            The exit status of the container that stopped; `0` for a clean stop.

        Raises:
            ValueError: `services` is empty, or `timeout` is not positive and
                finite. Raised before the CLI is resolved.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; the containers keep running.
            DockerException: The CLI failed with output on stderr.
        """
        validate_timeout(timeout)
        selected = to_list(services)
        if not selected:
            raise ValueError("services must name at least one service")
        full_cmd = Command([*self.docker_compose_cmd, "wait"])
        full_cmd.add_flag("--down-project", down_project)
        full_cmd += selected
        try:
            run(full_cmd, timeout=timeout)
        except DockerTimeout:
            raise
        except DockerException as error:
            stderr = "" if error.stderr is None else error.stderr
            if stderr or error.return_code < 1:
                raise
            return error.return_code
        return 0

    def is_installed(self) -> bool:
        """Check whether successful Compose help output identifies the plugin.

        Returns:
            Whether the help output contains `compose`.

        Raises:
            DockerException: The CLI help command fails, including when the
                plugin is unavailable. This failure does not return `False`.
        """
        full_cmd = Command([*self.docker_cmd, "compose", "--help"])
        help_output = run(full_cmd)
        return "compose" in help_output
