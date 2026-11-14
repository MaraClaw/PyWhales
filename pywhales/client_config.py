"""Shared CLI configuration, argument builders, and inspection-backed handles.

Executable lookup is lazy. Resource handles retain their originating config,
resolve mutable references to stable identities, and refresh cached snapshots
on demand. Inspect parsing follows that config's diagnostic confidentiality
policy.
"""

import os
import shutil
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, override

import pydantic

from . import utils
from ._environment import copy_environment, copy_environment_names
from ._execution_policy import PolicyExecutable
from ._json import dumps_compat, loads_compat
from ._types import JsonObject
from .enums import ClientTypeInput, ComposeProgressInput
from .telemetry import TelemetryObserver
from .utils import ValidPath, to_list

if TYPE_CHECKING:
    from .inventory import InspectionPolicy

CACHE_VALIDITY_PERIOD: float = 0.01


class ParsingError(Exception):
    pass


class ClientNotFoundError(Exception):
    pass


class Command(list[object]):
    """Mutable CLI argument list preserving values until subprocess launch.

    Helpers append options without shell quoting. Addition returns another
    Command so option helpers remain available after command composition.
    """

    def add_simple_arg(self, name: str, value: object) -> None:
        """Append an option and value unless the value is None.

        Args:
            name: Option spelling to append.
            value: Value to retain unchanged; zero, false, and empty strings are
                not omitted.
        """
        if value is not None:
            self.extend([name, value])

    def add_flag(self, name: str, value: bool) -> None:
        """Append a valueless option when enabled.

        Args:
            name: Flag spelling to append.
            value: Whether to include the flag.
        """
        if value:
            self.append(name)

    def add_args_iterable(self, arg_name: str, values: Iterable[object]) -> None:
        """Append a repeated option for each value in iteration order.

        Args:
            arg_name: Option spelling repeated before every value.
            values: Values to consume; an empty iterable appends nothing.
        """
        for value in values:
            self.extend([arg_name, value])

    def add_args_iterable_or_single(
        self, arg_name: str, iterable_or_single: object
    ) -> None:
        """Append repeated options after normalizing a scalar or iterable.

        Args:
            arg_name: Option spelling repeated before every value.
            iterable_or_single: Scalar or iterable accepted by to_list; strings
                and bytes remain single values rather than being split.
        """
        for value in to_list(iterable_or_single):
            self.extend([arg_name, value])

    def add_args_mapping(
        self, arg_name: str, mapping: Mapping[str, object], *, separator: str = "="
    ) -> None:
        """Append repeated options containing formatted mapping entries.

        Args:
            arg_name: Option spelling repeated before each entry.
            mapping: Entries emitted in mapping iteration order.
            separator: Text joining each key and value; defaults to ``=``.
        """
        self.add_args_iterable(
            arg_name, utils.format_mapping_for_cli(mapping, separator)
        )

    @override
    def __add__[ElementT](self, other: list[ElementT]) -> Command:
        """Return a Command containing this list followed by the other list.

        Args:
            other: Arguments to append without mutating either input list.

        Returns:
            A new command retaining the original argument objects.
        """
        return Command(super().__add__(other))


@dataclass
class ClientConfig:
    """Mutable settings shared by command groups and originating resources.

    Construction validates stream budgets without looking up the CLI or
    contacting a daemon. Executable-prefix lookup is cached on first use;
    command globals are rebuilt from current settings for each command.

    Attributes:
        config: Client configuration directory; ``None`` leaves CLI selection.
        context: Context override, omitted when ``None``.
        debug: Whether to emit the Docker debug flag; only true emits it.
        host: Daemon endpoint override, omitted when ``None``.
        log_level: Docker CLI logging level, omitted when ``None``.
        tls: Whether to emit the TLS flag; only true emits it.
        tlscacert: Trusted CA certificate path, omitted when ``None``.
        tlscert: Client certificate path, omitted when ``None``.
        tlskey: Client private-key path, omitted when ``None``.
        tlsverify: Whether to emit TLS verification; only true emits it.
        compose_files: Ordered Compose configuration files.
        compose_profiles: Compose profiles enabled through repeated options.
        compose_env_files: Ordered environment files; later files override
            earlier values according to Compose semantics.
        compose_project_name: Compose project-name override.
        compose_project_directory: Compose project-directory override.
        compose_compatibility: Whether to emit the Compose compatibility flag.
        client_call: Executable followed by any fixed prefix arguments.
        client_type: Declared Docker-compatible client behavior, not an
            executable autodetection result.
        compose_dry_run: Whether Compose commands request a dry run.
        compose_parallel: Compose concurrency limit; ``None`` omits the option.
        compose_progress: Default Compose progress mode; ``None`` omits it.
        image_stream_buffer_bytes: Independent pull/push byte-budget override;
            ``None`` falls back to the general budget at operation time.
        stream_buffer_bytes: Positive stream byte budget, or ``None`` for
            unbounded output. Queue payloads, unfinished records, and stderr
            history have separate limits, not one total-memory cap.
        raw_diagnostics: Per-client opt-in to confidential automatic diagnostic
            values. Safe defaults do not redact returned data or raw exception
            attributes, uncaptured terminal output, or caller-owned logging.
        telemetry_observer: Optional sanitized launch/completion callback, frozen
            by reference for each launch. Raw diagnostics never relax its schema.
        environment: Copied child environment overlay, hidden from repr but
            included in equality. Mutations affect subsequent independent launches.
        default_timeout: Positive finite seconds per CLI launch, or None for no deadline.
        strict_env_transport: Reject container envs and build args requiring literal
            argv fallback. Defaults to false for compatibility.
        inherit_environment: Inherit the parent environment for child launches;
            false retains only the platform base and explicit overlay.
        strict_workload_errors: Keep marked workload failures as DockerException.
        inherited_environment_names: Copied immutable ambient names, included in
            equality. A supplied collection replaces inherit_environment selection;
            empty inherits nothing and None preserves legacy inheritance. Explicit
            overlays still apply. This does not confine configuration-file access.
        compose_all_resources: Whether to emit the Compose option that includes
            resources no service uses; only true emits it.
        compose_ansi: Compose control-character mode (``"never"``, ``"always"``
            or ``"auto"``); ``None`` omits the option.
    """

    config: ValidPath | None = None
    context: str | None = None
    debug: bool | None = None
    host: str | None = None
    log_level: str | None = None
    tls: bool | None = None
    tlscacert: ValidPath | None = None
    tlscert: ValidPath | None = None
    tlskey: ValidPath | None = None
    tlsverify: bool | None = None
    client_binary_path: ValidPath | None = None
    compose_files: list[ValidPath] = field(default_factory=list)
    compose_profiles: list[str] = field(default_factory=list)
    compose_env_files: Sequence[ValidPath] = field(default_factory=list)
    compose_project_name: str | None = None
    compose_project_directory: ValidPath | None = None
    compose_compatibility: bool | None = None
    client_call: list[str] = field(default_factory=lambda: ["docker"])
    client_type: ClientTypeInput = "unknown"
    _client_call_with_path: list[Path | str] | None = None
    compose_dry_run: bool = False
    compose_parallel: int | None = None
    compose_progress: ComposeProgressInput | None = None
    image_stream_buffer_bytes: int | None = None
    stream_buffer_bytes: int | None = None
    raw_diagnostics: bool = False
    telemetry_observer: TelemetryObserver | None = field(default=None, repr=False)
    environment: Mapping[str, str] | None = field(default=None, repr=False)
    default_timeout: float | None = None
    strict_env_transport: bool = False
    inherit_environment: bool = True
    strict_workload_errors: bool = False
    inherited_environment_names: Collection[str] | None = None
    compose_all_resources: bool = False
    compose_ansi: Literal["never", "always", "auto"] | None = None

    def __post_init__(self) -> None:
        """Validate and copy environment settings and budgets without probing the CLI.

        Raises:
            ValueError: If a budget or default timeout is invalid, including
                boolean values, or an environment entry or inherited name is invalid.
            TypeError: If the environment is not a mapping of string keys and values,
                or inherited names are not a collection of strings.
        """
        if self.environment is not None:
            self.environment = copy_environment(self.environment)
        if self.inherited_environment_names is not None:
            self.inherited_environment_names = copy_environment_names(
                self.inherited_environment_names
            )
        utils.validate_timeout(self.default_timeout)
        for name, budget in (
            ("image_stream_buffer_bytes", self.image_stream_buffer_bytes),
            ("stream_buffer_bytes", self.stream_buffer_bytes),
        ):
            if budget is not None and (type(budget) is not int or budget <= 0):
                raise ValueError(f"{name} must be a positive integer or None")

    def get_client_call_with_path(self) -> list[Path | str]:
        """Resolve and cache the executable prefix on first use.

        Returns:
            The cached list containing the resolved executable path and fixed
            prefix arguments. Subsequent calls reuse this list.

        Raises:
            ClientNotFoundError: If the first lookup cannot find the executable.
        """
        if self._client_call_with_path is None:
            self._client_call_with_path = [
                Path(self._get_docker_path()),
                *self.client_call[1:],
            ]

        return self._client_call_with_path

    def _get_docker_path(self) -> str:
        """Return the executable path found for the first client_call argument.

        Raises:
            ClientNotFoundError: If executable lookup cannot resolve the command.
        """
        which_result = shutil.which(self.client_call[0])
        if which_result is None:
            raise ClientNotFoundError(
                f"The binary '{self.client_call[0]}' could not be found on your PATH. "
                "Please ensure that your PATH has the directory of the binary you're looking for. "
                "You can use `print(os.environ['PATH'])` to verify what directories are in your PATH."
            )
        return which_result

    @property
    def docker_cmd(self) -> Command:
        """Return current Docker globals with the originating policy carrier."""
        result = Command(self.get_client_call_with_path())
        result[0] = PolicyExecutable(str(result[0]), self)

        if self.config is not None:
            result += ["--config", self.config]

        if self.context is not None:
            result += ["--context", self.context]

        if self.debug is True:
            result.append("--debug")

        if self.host is not None:
            result += ["--host", self.host]

        if self.log_level is not None:
            result += ["--log-level", self.log_level]

        if self.tls is True:
            result.append("--tls")

        if self.tlscacert is not None:
            result += ["--tlscacert", self.tlscacert]

        if self.tlscert is not None:
            result += ["--tlscert", self.tlscert]

        if self.tlskey is not None:
            result += ["--tlskey", self.tlskey]

        if self.tlsverify is True:
            result.append("--tlsverify")

        return result

    @property
    def docker_compose_cmd(self) -> Command:
        """Return Docker and Compose globals before a Compose subcommand."""
        return self.get_compose_command()

    def get_compose_command(self, *, progress: str | None = None) -> Command:
        """Build Compose globals, optionally overriding progress for one command.

        Environment files are emitted in their configured order.

        Args:
            progress: Per-command progress override; ``None`` uses the configured
                default without mutating it.

        Returns:
            The executable prefix and Docker globals, followed by ``compose``
            and Compose globals ready for a subcommand.

        Raises:
            ClientNotFoundError: If initial executable lookup fails.
        """
        base_cmd = Command([*self.docker_cmd, "compose"])
        base_cmd.add_args_iterable_or_single("--file", self.compose_files)
        base_cmd.add_args_iterable_or_single("--profile", self.compose_profiles)
        base_cmd.add_args_iterable("--env-file", self.compose_env_files)

        base_cmd.add_simple_arg("--project-name", self.compose_project_name)
        base_cmd.add_simple_arg("--project-directory", self.compose_project_directory)
        base_cmd.add_flag("--compatibility", self.compose_compatibility is True)
        base_cmd.add_flag("--dry-run", self.compose_dry_run)
        base_cmd.add_simple_arg("--parallel", self.compose_parallel)
        base_cmd.add_simple_arg(
            "--progress", self.compose_progress if progress is None else progress
        )
        base_cmd.add_flag("--all-resources", self.compose_all_resources)
        base_cmd.add_simple_arg("--ansi", self.compose_ansi)
        return base_cmd


class DockerCLICaller:
    """Command-group base retaining the originating shared client config.

    Attributes:
        client_config: Config retained by reference for commands and diagnostics.
    """

    def __init__(self, client_config: ClientConfig) -> None:
        """Retain a config without looking up or launching its executable.

        Args:
            client_config: Settings shared with the originating client.
        """
        self.client_config = client_config

    @property
    def docker_cmd(self) -> Command:
        """Return a fresh Docker prefix using current shared settings."""
        return self.client_config.docker_cmd

    @property
    def docker_compose_cmd(self) -> Command:
        """Return a fresh Compose prefix using current shared settings."""
        return self.client_config.docker_compose_cmd


class ReloadableObject[InspectResultT, ReferenceT: str | None = str](DockerCLICaller):
    """Resource identity with a short-lived, inspection-backed snapshot cache.

    Resolve mutable references once, then inspect using the stored immutable
    identity. Snapshot access can run the CLI when data is absent or at least
    CACHE_VALIDITY_PERIOD seconds old on a monotonic clock. A hydrated listing
    pins its snapshots instead: a pinned snapshot never expires by age and
    ends at `reload()` or when a mutating call releases it. Copying or
    unpickling a handle keeps its snapshot but drops freshness and the pin.
    Equality includes config equality as well as identity, rather than
    comparing display names alone.
    """

    def __init__(
        self,
        client_config: ClientConfig,
        id_in_inspect: str,
        reference_or_id: ReferenceT,
        is_immutable_id: bool = False,
    ) -> None:
        """Bind a resource to its config and resolve its stable identity.

        Args:
            client_config: Originating config retained by reference.
            id_in_inspect: Snapshot attribute holding the immutable identity.
            reference_or_id: Reference to resolve, or a known immutable identity.
            is_immutable_id: Skip initial inspection when true; later snapshot
                access still fetches data on demand.

        Raises:
            ClientNotFoundError: If initial inspection cannot resolve the CLI.
            DockerException: If required initial inspection fails.
            ParsingError: If the concrete resource cannot parse its inspection.
        """
        super().__init__(client_config)
        self._last_refreshed_time = datetime.min
        self._refreshed_at: float | None = None
        self._pinned = False
        self._inspect_result: InspectResultT | None = None
        self._id_in_inspect = id_in_inspect
        if is_immutable_id:
            self._immutable_id: ReferenceT = reference_or_id
        else:
            inspect_result = self._fetch_and_parse_inspect_result(reference_or_id)
            self._set_inspect_result(inspect_result)
            self._immutable_id = getattr(inspect_result, id_in_inspect)

    @override
    def __eq__(self, other: object) -> bool:
        """Compare stored identities and config equality without refreshing.

        Args:
            other: Object to compare against this resource handle.

        Returns:
            Whether both objects are reloadable handles with equal identities
            and configs.
        """
        if not isinstance(other, ReloadableObject):
            return False
        return (
            self._get_immutable_id() == other._get_immutable_id()
            and self.client_config == other.client_config
        )

    @override
    def __str__(self) -> str:
        """Return the stored identity without inspecting the resource.

        Raises:
            TypeError: If this resource has no string identity.
        """
        reference = self._get_immutable_id()
        if reference is None:
            raise TypeError("__str__ returned non-string (type NoneType)")
        return reference

    def __setstate__(self, state: dict[str, object]) -> None:
        """Restore a copied or unpickled handle with its freshness and pin reset.

        Monotonic readings are meaningless outside the process that took them,
        so the snapshot is kept but counts as expired until the next read.

        Args:
            state: Instance attributes captured by copying or pickling.
        """
        self.__dict__.update(state)
        self._last_refreshed_time = datetime.min
        self._refreshed_at = None
        self._pinned = False

    def _needs_reload(self) -> bool:
        """Return whether the snapshot is absent, aged out, or from the future.

        The age comes from a monotonic clock and is compared with the current
        `CACHE_VALIDITY_PERIOD`. A negative age counts as expired.
        """
        if self._refreshed_at is None:
            return True
        age = monotonic() - self._refreshed_at
        return age < 0 or age >= CACHE_VALIDITY_PERIOD

    def reload(self) -> None:
        """Inspect the stored identity and replace the cached snapshot now.

        Failed fetches or parsing leave the existing snapshot and timestamp
        unchanged. The resource itself is not modified by inspection.
        """
        self._reload()

    def _reload(self) -> InspectResultT:
        """Fetch, cache, and return a snapshot for the stored identity."""
        inspect_result = self._fetch_and_parse_inspect_result(self._immutable_id)
        if self._immutable_id is not None and (
            getattr(inspect_result, self._id_in_inspect) != self._immutable_id
        ):
            raise ParsingError("Inspect identity does not match stored identity")
        self._set_inspect_result(inspect_result)
        return inspect_result

    def _fetch_and_parse_inspect_result(self, reference: ReferenceT) -> InspectResultT:
        """Define the subclass hook for fetching one parsed snapshot.

        Args:
            reference: CLI reference or stored identity to inspect.

        Returns:
            The snapshot supplied by a concrete implementation.

        Raises:
            NotImplementedError: When this base hook is called directly.
        """
        raise NotImplementedError

    def _get_inspect_result(self) -> InspectResultT:
        """Return cached data, fetching when it is absent or expired.

        A pinned snapshot is returned without consulting its age.
        """
        if self._inspect_result is None or (not self._pinned and self._needs_reload()):
            return self._reload()
        return self._inspect_result

    def _set_inspect_result(self, inspect_result: InspectResultT) -> None:
        """Install an unpinned snapshot and mark its refresh time as now.

        Publication through this setter, including `reload()` and bulk reload,
        also ends any pin; only `_set_pinned_inspect_result` pins.

        Args:
            inspect_result: Already parsed snapshot to publish without fetching.
        """
        self._inspect_result = inspect_result
        self._last_refreshed_time = datetime.now()
        self._refreshed_at = monotonic()
        self._pinned = False

    def _set_pinned_inspect_result(self, inspect_result: InspectResultT) -> None:
        """Install a snapshot that stays current until reloaded or released.

        Args:
            inspect_result: Already parsed snapshot to publish without fetching.
        """
        self._set_inspect_result(inspect_result)
        self._pinned = True

    def _release_pin(self) -> None:
        """Expire a pinned snapshot so the next read inspects again.

        An unpinned handle keeps its ordinary age-based expiry untouched.
        """
        if self._pinned:
            self._pinned = False
            self._refreshed_at = None

    def _get_immutable_id(self) -> ReferenceT:
        """Return the stored identity without triggering snapshot refresh."""
        return self._immutable_id

    @override
    def __hash__(self) -> int:
        """Return a hash of the stored identity without inspecting the resource."""
        # maybe we can do better.
        return hash(self._get_immutable_id())


class ReloadableObjectFromJson[InspectResultT, ReferenceT: str | None = str](
    ReloadableObject[InspectResultT, ReferenceT]
):
    """Inspection-backed handle with JSON parsing and diagnostic safeguards.

    Safe failures suppress input-bearing validation chains and response dumps.
    Raw diagnostic opt-in may retain those chains and write invalid snapshots
    to temporary files, which must be treated as confidential.
    """

    def _parse_single_inspect_json(self, json_str: str) -> JsonObject:
        """Return the single object in an inspect array, respecting diagnostic policy.

        Args:
            json_str: Captured scalar inspect response.

        Raises:
            ParsingError: If the response is malformed or not a singleton object array.
            pydantic.ValidationError: If the object contains invalid JSON values.
        """
        try:
            result = loads_compat(json_str)
        except JSONDecodeError as err:
            if self.client_config.raw_diagnostics:
                raise ParsingError("Invalid inspect JSON response") from err
            raise ParsingError("Invalid inspect JSON response") from None
        if (
            not isinstance(result, list)
            or len(result) != 1
            or not isinstance(result[0], dict)
        ):
            raise ParsingError("Expected one inspect JSON object")
        return utils.custom_parse_object_as(dict[str, pydantic.JsonValue], result[0])

    def _fetch_inspect_result_json(self, reference: ReferenceT) -> JsonObject:
        """Define the subclass hook for fetching one JSON inspection object.

        Args:
            reference: CLI reference or stored identity to inspect.

        Returns:
            The JSON object supplied by a concrete implementation.

        Raises:
            NotImplementedError: When this base hook is called directly.
        """
        raise NotImplementedError

    def _parse_json_object(self, json_object: JsonObject) -> InspectResultT:
        """Define the subclass hook for parsing an inspection object.

        Args:
            json_object: Metadata to convert to the concrete resource model.

        Returns:
            The snapshot supplied by a concrete implementation.

        Raises:
            NotImplementedError: When this base hook is called directly.
        """
        raise NotImplementedError

    @override
    def _fetch_and_parse_inspect_result(self, reference: ReferenceT) -> InspectResultT:
        """Fetch and parse one snapshot using the client's diagnostic policy.

        Args:
            reference: CLI reference or stored identity to inspect.

        Returns:
            The parsed snapshot without publishing it to this handle's cache.

        Raises:
            ParsingError: If safe-mode fetching or model parsing rejects JSON.
            pydantic.ValidationError: If raw-mode fetching rejects the response.
            DockerException: If the concrete inspect command fails.
        """
        try:
            json_object = self._fetch_inspect_result_json(reference)
        except pydantic.ValidationError:
            if self.client_config.raw_diagnostics:
                raise
            raise ParsingError("Invalid inspect JSON response") from None
        return self._parse_inspect_result(json_object)

    def _parse_inspect_results(
        self, json_str: str, references: Sequence[str]
    ) -> list[InspectResultT]:
        """Validate a whole batch's JSON, count, and resolved identities.

        Repeated references must resolve consistently. This does not require
        every distinct reference to equal its resolved identity; namespace-
        specific callers apply those checks separately.

        Args:
            json_str: Captured JSON array returned by batch inspection.
            references: Requested references in response order.

        Returns:
            Parsed snapshots in the original order, without publishing caches.

        Raises:
            ParsingError: If JSON or models are invalid, counts differ, an
                identity is empty, or a repeated reference resolves differently.
                Raw diagnostics may expose validation chains or response dumps.
        """
        try:
            json_objects = pydantic.TypeAdapter(list[JsonObject]).validate_json(
                json_str
            )
        except pydantic.ValidationError as err:
            if self.client_config.raw_diagnostics:
                raise ParsingError("Invalid batch inspect JSON response") from err
            raise ParsingError("Invalid batch inspect JSON response") from None
        if len(json_objects) != len(references):
            raise ParsingError(
                f"Expected {len(references)} inspect results, received {len(json_objects)}"
            )
        results = [self._parse_inspect_result(item) for item in json_objects]
        resolved: dict[str, str] = {}
        for reference, result in zip(references, results, strict=True):
            identity = getattr(result, self._id_in_inspect)
            if not isinstance(identity, str) or not identity:
                detail = (
                    f" for {reference!r}" if self.client_config.raw_diagnostics else ""
                )
                raise ParsingError(f"Missing inspect identity{detail}")
            if reference in resolved and resolved[reference] != identity:
                detail = (
                    f" for {reference!r}" if self.client_config.raw_diagnostics else ""
                )
                raise ParsingError(f"Inconsistent inspect identity{detail}")
            resolved[reference] = identity
        return results

    def _parse_inspect_result(self, json_object: JsonObject) -> InspectResultT:
        """Parse one model and report validation failures according to policy.

        Args:
            json_object: Inspection metadata passed to the concrete model parser.

        Returns:
            The parsed snapshot, without changing the resource cache.

        Raises:
            ParsingError: If model validation fails. Safe mode withholds input
                details and suppresses validation chaining. Raw mode writes a
                temporary JSON dump and chains the input-bearing validation
                error; both can contain confidential data.
        """
        try:
            return self._parse_json_object(json_object)
        except pydantic.ValidationError as err:
            if not self.client_config.raw_diagnostics:
                raise ParsingError(
                    "There was an error parsing the JSON response from the Docker daemon. "
                    "This is a bug with pywhales itself. Response details are withheld; "
                    "enable raw_diagnostics on a troubleshooting client for details."
                ) from None
            fd, json_response_file = tempfile.mkstemp(suffix=".json", text=True)
            with os.fdopen(fd, "w") as f:
                f.write(dumps_compat(json_object, indent=2))

            raise ParsingError(
                f"There was an error parsing the json response from the Docker daemon. \n"
                f"This is a bug with pywhales itself. Please head to \n"
                f"https://github.com/MaraClaw/PyWhales/issues \n"
                f"and open an issue. You should copy this error message and \n"
                f"the json response from the Docker daemon. The json response was put \n"
                f"in {json_response_file} because it's a bit too big to be printed \n"
                f"on the screen. Make sure that there are no sensitive data in the \n"
                f"json file before copying it in the github issue."
            ) from err


def bulk_reload(
    docker_objects: Sequence[object], *, policy: InspectionPolicy | None = None
) -> None:
    """Stage every resource group's snapshots before publishing cache updates.

    Supported resources are containers, images, networks, volumes, nodes,
    services, configs, secrets, tasks, plugins, and contexts. Group by exact
    config instance and kind. Repeated handles publish once; distinct handles
    for one identity each refresh. Failure during staging leaves original caches
    and timestamps unchanged. Publication is sequential, not atomic for readers
    and not a daemon-side transaction.

    Args:
        docker_objects: Resource handles to inspect; empty input is a no-op.
        policy: Optional inspection limits, intersected with an active observation.

    Raises:
        TypeError: If a kind is unsupported or staged resource kinds differ.
        ParsingError: If inspection metadata or identities are invalid.
        ValueError: If a group returns an unexpected number of handles.
        ClientNotFoundError: If a required executable cannot be resolved.
        DockerException: If a group's inspect command fails.
    """
    from ._bulk_reload import bulk_reload as reload_snapshots

    reload_snapshots(docker_objects, policy=policy)
