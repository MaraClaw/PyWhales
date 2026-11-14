"""Docker Buildx commands, builder handles, and owned build-output streams.

Builds and Bake operations invoke the configured Docker CLI and Buildx plugin.
Registry manifest operations are exposed through the imagetools command group.
"""

from __future__ import annotations

import builtins
import datetime as dt
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import (
    Final,
    Literal,
    Self,
    overload,
    override,
)

from pydantic import JsonValue, TypeAdapter

import pywhales.components.buildx.dap
import pywhales.components.buildx.replay
import pywhales.components.image.cli_wrapper
from pywhales._environment import container_environment
from pywhales._json import loads_compat
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ParsingError,
    ReloadableObject,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components._json_output import JsonOutputParser
from pywhales.components.buildx.history import BuildxHistoryCLI
from pywhales.components.buildx.imagetools.cli_wrapper import ImagetoolsCLI
from pywhales.components.buildx.models import (
    BuilderInspectResult,
    BuilderNode,
    BuildxDiskUsage,
)
from pywhales.components.buildx.policy import BuildxPolicyCLI
from pywhales.enums import BuildxBuildCall, BuildxProgressMode
from pywhales.enums import BuildxCall as BuildxCall
from pywhales.enums import BuildxProgress as BuildxProgress
from pywhales.enums import BuildxReportCall as BuildxReportCall
from pywhales.utils import (
    MappedProcessStream,
    ValidPath,
    format_filters_for_cli,
    format_mapping_for_cli,
    run,
    stream_stdout_and_stderr,
    to_list,
    to_seconds,
    validate_timeout,
)

type BakeResult = dict[str, dict[str, dict[str, JsonValue]]]
type BuildxOptions = Mapping[str, str | Path]
type BuildxPruneFilter = tuple[str, str]
type BakeListKind = Literal["targets", "variables"]
type BuildxDuration = int | dt.timedelta
type BuildxDialProgress = (
    BuildxProgressMode | Literal["auto", "none", "plain", "rawjson", "tty"]
)
_BAKE_RESULT_ADAPTER: Final = TypeAdapter(BakeResult)


def _parse_builder_line(line: str) -> BuilderInspectResult:
    return BuilderInspectResult(**loads_compat(line))


def _parse_builder_entry(line: str) -> tuple[str, BuilderInspectResult]:
    result = _parse_builder_line(line)
    if result.name is None:
        raise ValueError("Builder listing entry has no name")
    return result.name, result


class Builder(ReloadableObject[BuilderInspectResult]):
    """Reloadable builder handle retaining its originating client configuration.

    Inspection-backed properties can invoke Buildx to refresh their snapshots.
    Context-manager exit removes the builder, including when the body raises.
    """

    def __init__(
        self,
        client_config: ClientConfig,
        reference: str,
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
        with resource_cleanup(exc_val, "Builder body and cleanup both failed"):
            self.remove()

    @override
    def _fetch_and_parse_inspect_result(self, reference: str) -> BuilderInspectResult:
        full_cmd = Command(
            [*self.docker_cmd, "buildx", "ls", "--format", "{{json . }}"]
        )
        inspect_str = run(full_cmd)
        raw = self.client_config.raw_diagnostics
        parser = JsonOutputParser("builder listing", raw)
        for line in inspect_str.splitlines():
            builder_inspect_result = parser.parse(_parse_builder_line, line)
            if builder_inspect_result.name == reference:
                return builder_inspect_result
        label = f"the name {reference}" if raw else "that name"
        raise ValueError(
            f"Could not find a builder with {label}, this should "
            f"never happen unless you are quickly creating and deleting builders. "
            f"Please open an issue at https://github.com/MaraClaw/PyWhales/issues"
        )

    @property
    def name(self) -> str:
        return self._get_immutable_id()

    @property
    def driver(self) -> str | None:
        return self._get_inspect_result().driver

    @property
    def last_activity(self) -> dt.datetime | None:
        return self._get_inspect_result().last_activity

    @property
    def dynamic(self) -> bool | None:
        return self._get_inspect_result().dynamic

    @property
    def nodes(self) -> list[BuilderNode] | None:
        return self._get_inspect_result().nodes

    @override
    def __repr__(self) -> str:
        return f"pywhales.Builder(name='{self.name}', driver='{self.driver}')"

    def remove(
        self,
        *,
        force: bool = False,
        keep_daemon: bool = False,
        keep_state: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> None:
        """Remove this builder using its originating client configuration.

        Context-manager exit also calls this method without options. The handle
        does not recreate a removed builder; subsequent commands can fail if it
        no longer exists.

        Args:
            force: Do not prompt for confirmation.
            keep_daemon: Keep the BuildKit daemon running.
            keep_state: Keep BuildKit state.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`.
            debug: Forward Buildx `--debug` immediately after `buildx rm`.
        """
        BuildxCLI(self.client_config).remove(
            self,
            force=force,
            keep_daemon=keep_daemon,
            keep_state=keep_state,
            status_timeout=status_timeout,
            debug=debug,
        )


ValidBuilder = str | Builder


def parse_builder_listing(
    output: str, *, raw_diagnostics: bool = False
) -> dict[str, BuilderInspectResult]:
    """Parse a whole builder listing into snapshots keyed by exact builder name.

    Only byte-identical lines collapse, because the template also renders node
    rows. The listing is validated completely before any snapshot is returned.
    Safe mode withholds names and doesn't chain input-bearing parse errors.

    Args:
        output: Captured line-delimited JSON from `buildx ls --format "{{json . }}"`.
        raw_diagnostics: Whether to include names and chain the raw parse error.

    Returns:
        Snapshots in listing order, each under its nonempty name.

    Raises:
        ParsingError: If any line is not a builder object, has a null or empty
            name, or shares a name with a different line.
    """
    snapshots: dict[str, BuilderInspectResult] = {}
    for line in dict.fromkeys(output.splitlines()):
        try:
            snapshot = BuilderInspectResult.model_validate_json(line, strict=True)
        except ValueError as err:
            if raw_diagnostics:
                raise ParsingError("Invalid builder listing line") from err
            raise ParsingError("Invalid builder listing line") from None
        name = snapshot.name
        if name is None or name == "":
            raise ParsingError("Builder listing entry has no name")
        if name in snapshots:
            detail = f" for {name!r}" if raw_diagnostics else ""
            raise ParsingError(f"Conflicting builder listing entries{detail}")
        snapshots[name] = snapshot
    return snapshots


def list_builder_snapshots(
    client_config: ClientConfig,
) -> dict[str, BuilderInspectResult]:
    """Run one builder listing and strictly parse it; no names enter argv."""
    output = run(
        Command([*client_config.docker_cmd, "buildx", "ls", "--format", "{{json . }}"])
    )
    return parse_builder_listing(output, raw_diagnostics=client_config.raw_diagnostics)


class BuildxCLI(DockerCLICaller):
    """Buildx command group sharing the client's connection and execution policy."""

    def __init__(self, client_config: ClientConfig) -> None:
        super().__init__(client_config)
        self.imagetools = ImagetoolsCLI(self.client_config)
        self.dap = pywhales.components.buildx.dap.BuildxDapCLI(self.client_config)
        self.history = BuildxHistoryCLI(self.client_config)
        self.policy = BuildxPolicyCLI(self.client_config)
        self.replay = pywhales.components.buildx.replay.BuildxReplayCLI(
            self.client_config
        )

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxBuildCall | None = None,
        jobs: int | str | None = None,
        return_config: Literal[True] = True,
        allow: str | Sequence[str] = (),
        list: None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> BakeResult: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        *,
        stream_logs: Literal[True],
        remote_definition: str | None = None,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: bool = True,
        allow: str | Sequence[str] = (),
        list: BakeListKind | None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxBuildCall | None = None,
        jobs: int | str | None = None,
        return_config: Literal[False],
        allow: str | Sequence[str] = (),
        list: None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxReportCall,
        jobs: int | str | None = None,
        return_config: bool = True,
        allow: str | Sequence[str] = (),
        list: BakeListKind | None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: Literal[True] = True,
        allow: str | Sequence[str] = (),
        list: None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> BakeResult | str: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: Literal[False],
        allow: str | Sequence[str] = (),
        list: None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> str | None: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str],
        builder: ValidBuilder | None,
        files: ValidPath | Sequence[ValidPath],
        load: bool,
        cache: bool,
        print: bool,
        progress: BuildxProgress,
        pull: bool,
        push: bool,
        set: Mapping[str, str],
        variables: Mapping[str, str],
        metadata_file: ValidPath | None,
        stream_logs: Literal[True],
        remote_definition: str | None = None,
        *,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: bool = True,
        allow: str | Sequence[str] = (),
        list: BakeListKind | None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    @overload
    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: Literal[False] = False,
        remote_definition: str | None = None,
        *,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: bool = True,
        allow: str | Sequence[str] = (),
        list: BakeListKind,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> str: ...

    def bake(
        self,
        targets: str | Sequence[str] = (),
        builder: ValidBuilder | None = None,
        files: ValidPath | Sequence[ValidPath] = (),
        load: bool = False,
        cache: bool = True,
        print: bool = False,
        progress: BuildxProgress = "auto",
        pull: bool = False,
        push: bool = False,
        set: Mapping[str, str] = MappingProxyType({}),
        variables: Mapping[str, str] = MappingProxyType({}),
        metadata_file: ValidPath | None = None,
        stream_logs: bool = False,
        remote_definition: str | None = None,
        *,
        call: BuildxCall | None = None,
        jobs: int | str | None = None,
        return_config: bool = True,
        allow: str | Sequence[str] = (),
        list: BakeListKind | None = None,
        policies: str | Sequence[str] = (),
        provenance: bool | Mapping[str, str] | None = None,
        sbom: bool | Mapping[str, str] | None = None,
        variables_set: Mapping[str, str] = MappingProxyType({}),
        debug: bool = False,
        timeout: float | None = None,
    ) -> BakeResult | str | MappedProcessStream[str] | None:
        """Build Bake targets or query their configuration or frontend reports.

        Work starts during the call. A normal blocking build runs Bake and then,
        by default, a separate invocation with the same options, targets, and
        variables plus `--print`. That follow-up queries configuration; it is not
        a snapshot captured from the build. Reports and streams skip this query.

        Streaming starts the local CLI before returning. Exhaust the returned
        stream, use its context manager, or call `close()` when stopping early.
        Closing cancels and reaps the local CLI, not daemon-side build work.

        Args:
            targets: Targets or groups of targets to build.
            builder: The builder to use.
            files: Build definition files, in CLI argument order.
            load: Request the Docker image exporter for the targets.
            cache: Whether to use the cache or not.
            print: Query configuration without building. Requires `return_config`
                and cannot be combined with streaming or a report call.
            progress: Progress rendering mode. `False` captures stderr instead
                of displaying it. Streaming requires a mode that emits logs.
            pull: Always try to pull a newer base image.
            push: Ask Bake to push target outputs to a registry.
            set: Overrides mapping target patterns and keys to values, such as
                `{"*.args.VERSION": "1"}`.
            variables: Environment overrides for Bake variables on each invocation.
            metadata_file: Local path for build-result metadata; `None` omits it.
            stream_logs: Return decoded output chunks instead of waiting for
                completion and returning configuration or report text.
            remote_definition: Remote context in which to find Bake files.
            call: Frontend method to invoke. `None` and `"build"` build normally;
                `"check"`, `"outline"` and `"targets"` return captured report text.
                Requires a Buildx version supporting `--call`.
            jobs: Target concurrency or execution mode, forwarded as `--jobs`.
                Requires a Buildx version supporting this flag. Zero is forwarded.
            return_config: When `False`, skip the extra configuration query after
                a blocking build and return `None`. Reports and streams do not
                query configuration. Cannot be combined with `print=True`.
            allow: Extra resource access granted to the build, such as
                `"network.host"`, `"security.insecure"` or `"fs=/path"`, passed
                as repeated `--allow` flags. Only grant them to trusted builds.
            list: Report kind, `"targets"` or `"variables"`, forwarded as
                `--list`. The call is a single launch that returns the table
                text, skips the build and configuration query, and ignores
                `return_config`. It conflicts with `print=True`.
            policies: Global policy evaluation options, such as
                `"strict=true,log-level=warn"`, passed as repeated `--policy`
                flags.
            provenance: Provenance attestation shorthand for every target:
                `True` or `False` is forwarded as `--provenance=true` or
                `--provenance=false`, a mapping as comma-joined `key=value`
                parameters. `None` leaves the Bake default unchanged.
            sbom: SBOM attestation shorthand for every target, encoded like
                `provenance`.
            variables_set: Bake variable values passed as repeated `--var
                name=value` flags. These differ from `variables`, which sets
                environment values for the CLI launch.
            debug: Forward Buildx `--debug` immediately after `buildx bake`.
                Debug output follows the launch's stderr handling and the return
                value is unchanged.
            timeout: Positive finite seconds per local CLI invocation, or `None`
                for no deadline. The build and follow-up configuration query each
                receive a fresh budget, not one shared operation-wide deadline.
                A stream's deadline advances even before or between iterations.

        Returns:
            Configuration from `--print` when `print=True`, or from the follow-up
            query after a normal blocking build with `return_config=True`.
            Blocking `check`, `outline`, and `targets` calls and `list` requests
            return report text, regardless of `return_config`. With
            `stream_logs=True`, return an owned stream of decoded output chunks
            with source labels discarded. A normal blocking build with
            `return_config=False` returns `None`.

        Raises:
            ValueError: `print=True` conflicts with streaming, a report call,
                `list`, or `return_config=False`, or timeout is Boolean,
                nonpositive, or nonfinite.
            DockerTimeout: A local CLI exceeds its deadline; the CLI is killed
                and reaped without guaranteeing cancellation of the build.
            DockerException: A local CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        report = call in ("check", "outline", "targets") or list is not None
        if print and stream_logs:
            raise ValueError(
                "Getting the config of the bake and streaming "
                "logs at the same time is not possible."
            )
        if print and not return_config:
            raise ValueError("print=True requires return_config=True.")
        if print and list is not None:
            raise ValueError("print=True cannot be combined with list.")
        if print and report:
            raise ValueError("print=True cannot be combined with a report call.")

        full_cmd = Command([*self.docker_cmd, "buildx", "bake"])

        full_cmd.add_flag("--debug", debug)
        full_cmd.add_flag("--no-cache", not cache)
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_flag("--load", load)
        full_cmd.add_flag("--pull", pull)
        full_cmd.add_flag("--push", push)
        full_cmd.add_flag("--print", print)
        full_cmd.add_simple_arg("--call", call)
        full_cmd.add_simple_arg("--jobs", jobs)
        full_cmd.add_args_iterable_or_single("--allow", allow)
        full_cmd.add_simple_arg("--list", list)
        full_cmd.add_args_iterable_or_single("--policy", policies)
        full_cmd += _attestation_option("--provenance", provenance)
        full_cmd += _attestation_option("--sbom", sbom)
        full_cmd.add_args_iterable_or_single(
            "--var", format_mapping_for_cli(variables_set)
        )
        if progress != "auto" and isinstance(progress, str):
            full_cmd += ["--progress", progress]
        for file in to_list(files):
            full_cmd.add_simple_arg("--file", file)
        full_cmd.add_args_iterable_or_single("--set", format_mapping_for_cli(set))
        if remote_definition is not None:
            full_cmd.append(remote_definition)
        if metadata_file is not None:
            full_cmd.add_simple_arg("--metadata-file", metadata_file)
        targets = to_list(targets)
        env = dict(variables)
        if print:
            return _BAKE_RESULT_ADAPTER.validate_json(
                run([*full_cmd, *targets], env=env, timeout=timeout)
            )
        elif stream_logs:
            return stream_buildx_logs([*full_cmd, *targets], env=env, timeout=timeout)
        else:
            output = run(
                [*full_cmd, *targets],
                capture_stderr=progress is False,
                env=env,
                timeout=timeout,
            )
            if report:
                return output
            if return_config:
                return _BAKE_RESULT_ADAPTER.validate_json(
                    run([*full_cmd, "--print", *targets], env=env, timeout=timeout)
                )

    @overload
    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None = None,
        cache_to: str | BuildxOptions | None = None,
        file: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: BuildxOptions = MappingProxyType({}),
        platforms: builtins.list[str] | None = None,
        progress: BuildxProgress = "auto",
        provenance: bool | dict[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        sbom: bool | dict[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        stream_logs: Literal[False] = False,
        *,
        call: BuildxBuildCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> pywhales.components.image.cli_wrapper.Image | None: ...

    @overload
    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None = None,
        cache_to: str | BuildxOptions | None = None,
        file: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: BuildxOptions = MappingProxyType({}),
        platforms: builtins.list[str] | None = None,
        progress: BuildxProgress = "auto",
        provenance: bool | dict[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        sbom: bool | dict[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        *,
        stream_logs: Literal[True],
        call: BuildxCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    @overload
    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None = None,
        cache_to: str | BuildxOptions | None = None,
        file: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: BuildxOptions = MappingProxyType({}),
        platforms: builtins.list[str] | None = None,
        progress: BuildxProgress = "auto",
        provenance: bool | dict[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        sbom: bool | dict[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        stream_logs: Literal[False] = False,
        *,
        call: BuildxReportCall,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None = None,
        cache_to: str | BuildxOptions | None = None,
        file: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: BuildxOptions = MappingProxyType({}),
        platforms: builtins.list[str] | None = None,
        progress: BuildxProgress = "auto",
        provenance: bool | dict[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        sbom: bool | dict[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        stream_logs: Literal[False] = False,
        *,
        call: BuildxCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> pywhales.components.image.cli_wrapper.Image | str | None: ...

    @overload
    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str],
        allow: Sequence[str],
        attest: dict[str, str] | None,
        build_args: Mapping[str, str],
        build_contexts: Mapping[str, str | ValidPath],
        builder: ValidBuilder | None,
        cache: bool,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None,
        cache_to: str | BuildxOptions | None,
        file: ValidPath | None,
        labels: Mapping[str, str],
        load: bool,
        metadata_file: ValidPath | None,
        network: str | None,
        output: BuildxOptions,
        platforms: builtins.list[str] | None,
        progress: BuildxProgress,
        provenance: bool | dict[str, str] | None,
        pull: bool,
        push: bool,
        sbom: bool | dict[str, str] | None,
        secrets: str | Sequence[str],
        ssh: str | None,
        tags: str | Sequence[str],
        target: str | None,
        stream_logs: Literal[True],
        *,
        call: BuildxCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    def build(
        self,
        context_path: ValidPath,
        add_hosts: Mapping[str, str] = MappingProxyType({}),
        allow: Sequence[str] = (),
        attest: dict[str, str] | None = None,
        build_args: Mapping[str, str] = MappingProxyType({}),
        build_contexts: Mapping[str, str | ValidPath] = MappingProxyType({}),
        builder: ValidBuilder | None = None,
        cache: bool = True,
        cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None = None,
        cache_to: str | BuildxOptions | None = None,
        file: ValidPath | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        load: bool = False,
        metadata_file: ValidPath | None = None,
        network: str | None = None,
        output: BuildxOptions = MappingProxyType({}),
        platforms: builtins.list[str] | None = None,
        progress: BuildxProgress = "auto",
        provenance: bool | dict[str, str] | None = None,
        pull: bool = False,
        push: bool = False,
        sbom: bool | dict[str, str] | None = None,
        secrets: str | Sequence[str] = (),
        ssh: str | None = None,
        tags: str | Sequence[str] = (),
        target: str | None = None,
        stream_logs: bool = False,
        *,
        call: BuildxCall | None = None,
        no_cache_filter: str | Sequence[str] | None = None,
        annotations: str | Sequence[str] | None = None,
        cgroup_parent: str | None = None,
        policies: str | Sequence[str] = (),
        quiet: bool = False,
        resources: Mapping[str, str | int] = MappingProxyType({}),
        shm_size: int | str | None = None,
        ulimits: str | Sequence[str] = (),
        debug: bool = False,
        timeout: float | None = None,
    ) -> (
        pywhales.components.image.cli_wrapper.Image
        | str
        | MappedProcessStream[str]
        | None
    ):
        """Build an image with BuildKit or invoke a frontend report method.

        Alias: `docker.build(...)`

        Blocking builds recover one image only when the result is loaded and
        identifiable. The Docker driver uses a temporary image-ID file; other
        drivers use the first tag. An untagged Docker-container-driver load
        returns `None` even with `load=True`. Reports skip image recovery.

        Streaming starts the local CLI before returning and skips image recovery.
        Exhaust the stream, use its context manager, or call `close()` on early
        exit. Closing reaps the local CLI but does not guarantee that daemon-side
        builds stop. A bare loop break does not close the stream.

        Args:
            context_path: Local or remote build context accepted by Buildx.
            add_hosts: Build-host names mapped to addresses, such as
                `{"build.example": "192.0.2.1"}`.
            allow: Extra build entitlements, such as `"network.host"` or
                `"security.insecure"`; only grant them to trusted builds.
            attest: Attestation parameters, such as `{"type": "sbom"}`.
            build_args: Build-time variable names and values. Eligible values use
                the child environment; other entries stay literal unless
                strict_env_transport rejects them before lookup or inspection.
            build_contexts: Named additional contexts, including local directories,
                Git repositories, HTTP archives, `docker-image://` references,
                and `oci-layout://` paths supported by the installed Buildx.
            builder: Specify which builder to use.
            cache: Whether or not to use the cache
            cache_from: Loads the cache
                (if needed) from a registry `cache_from="user/app:cache"`  or
                a directory on the client `cache_from="type=local,src=path/to/dir"`.
                It's also possible to use a dict or list of dict form for this
                argument. e.g.
                `cache_from=dict(type="local", src="path/to/dir")`
            cache_to: Sends the resulting
                docker cache either to a registry `cache_to="user/app:cache"`,
                or to a local directory `cache_to="type=local,dest=path/to/dir"`.
                It's also possible to use a dict form for this argument. e.g.
                `cache_to=dict(type="local", dest="path/to/dir", mode="max")`
                Supported external cache backends with the Docker driver depend
                on the containerd image store being enabled. For GitHub Actions
                cache, `docker-container` is the conservative recommendation.
            file: Local Dockerfile path; `None` leaves selection to Buildx.
            labels: Image label names and values.
                `labels={"very-secure": "1", "needs-gpu": "0"}` for example.
            load: Request loading with the Docker exporter. This alone does not
                guarantee that an image handle can be recovered.
            metadata_file: Path where Buildx should write build metadata; `None`
                omits `--metadata-file`.
            network: Network mode for build execution; `None` omits the option.
            output: Output destination
                (format: `output={"type": "local", "dest": "path"}`
                Possible output types are
                `["local", "tar", "oci", "docker", "image", "registry"]`.
                See [this link](https://github.com/docker/buildx#-o---outputpath-typetypekeyvalue)
                for more details about each exporter.
            platforms: Target platforms when building the image, such as
                `platforms=["linux/amd64", "linux/arm64"]`
            progress: Progress rendering mode. `False` captures stderr instead
                of displaying it and cannot be combined with streaming.
            provenance: Enable, disable, or configure provenance attestations.
                `None` leaves the Buildx default unchanged.
            pull: Always attempt to pull a newer base image.
            push: Request the registry exporter.
            sbom: Shorthand for `attest={"type": "sbom"}`. Eg `sbom=True`.
            secrets: Secret specifications, such as "id=build,src=./build.secret".
                Only expose trusted local files to trusted builds.
            ssh: SSH agent socket or keys to expose to a trusted build, using
                Buildx's `default` or `<id>[=<socket>|<key>[,<key>]]` syntax.
            tags: Tag or tags to put on the resulting image.
            target: Set the target build stage to build.
            stream_logs: Return an owned stream of decoded output chunks instead
                of a recovered image or captured report.
            call: Frontend method to invoke. `None` and `"build"` build normally;
                `"check"`, `"outline"` and `"targets"` return captured report text
                without image recovery. Requires Buildx support for `--call`.
            no_cache_filter: Stage name(s) to rebuild without cache, passed as
                repeated `--no-cache-filter` flags.
            annotations: Image annotation(s), passed as repeated `--annotation`
                flags. Requires a Buildx version supporting these flags.
            cgroup_parent: Parent cgroup for `RUN` instructions; `None` omits it.
            policies: Policy configuration(s), such as
                `"filename=policy.rego,strict=true"`, passed as repeated
                `--policy` flags.
            quiet: Suppress build output and have Buildx print the image ID.
                Cannot be combined with `stream_logs`.
            resources: Resource limits for build containers, such as
                `{"memory": "2g", "cpu-quota": 50000}`, passed as repeated
                `--resource key=value` flags.
            shm_size: Shared memory size for build containers, in bytes or with a
                unit such as `"64m"`; `None` omits it.
            ulimits: Ulimit option(s) for build containers, such as
                `"nofile=1024:2048"`, passed as repeated `--ulimit` flags.
            debug: Forward Buildx `--debug` immediately after `buildx build`.
                Debug output follows the launch's stderr handling and the return
                value is unchanged.
            timeout: Positive finite seconds for the local build CLI, or `None`
                to inherit the client default. Builder inspection before launch and image
                inspection after completion are outside this budget. Streaming
                deadlines advance before and between iterations.

        Returns:
            One image handle, retaining the originating client configuration,
            for an identifiable loaded result, including builds with many tags.
            Return `None` for a build whose result is not loaded or recoverable.
            Blocking `check`, `outline`, and `targets` calls return report text.
            With `stream_logs=True`, return an owned stream whose text chunks
            combine stdout and stderr without source labels.

        Raises:
            ValueError: Streaming is combined with `progress=False`, `"tty"`,
                `"none"`, or `"quiet"` or with `quiet=True`, or `timeout` is not
                positive and finite. `quiet` is rejected before any lookup.
            DockerTimeout: The local build CLI exceeds its deadline; it is killed
                and reaped without guaranteeing cancellation of the build.
            DockerException: A Buildx or image-inspection command fails.
        """
        validate_timeout(timeout)
        if quiet and stream_logs:
            raise ValueError("quiet cannot be combined with stream_logs.")
        tags = to_list(tags)
        full_cmd, child_env = _build_command(
            self.client_config,
            ("buildx", "build"),
            add_hosts=add_hosts,
            allow=allow,
            attest=attest,
            build_args=build_args,
            build_contexts=build_contexts,
            builder=builder,
            cache=cache,
            cache_from=cache_from,
            cache_to=cache_to,
            cgroup_parent=cgroup_parent,
            file=file,
            labels=labels,
            load=load,
            metadata_file=metadata_file,
            network=network,
            output=output,
            platforms=platforms,
            policies=policies,
            progress=progress,
            provenance=provenance,
            pull=pull,
            push=push,
            quiet=quiet,
            resources=resources,
            sbom=sbom,
            secrets=secrets,
            shm_size=shm_size,
            ssh=ssh,
            tags=tags,
            target=target,
            ulimits=ulimits,
            call=call,
            no_cache_filter=no_cache_filter,
            annotations=annotations,
            debug=debug,
        )

        if stream_logs:
            if progress in (False, "tty", "none", "quiet"):
                raise ValueError(
                    "You want to stream logs, but it's not possible if a tty is used "
                    "as 'progress'. It's also not possible if 'progress' is False, "
                    "'none' or 'quiet', since those modes emit no logs. "
                    "Make sure the function arguments of 'docker.build' are "
                    "coherent."
                )

            full_cmd.append(context_path)
            return stream_buildx_logs(full_cmd, env=child_env, timeout=timeout)

        if call in ("check", "outline", "targets"):
            full_cmd.append(context_path)
            return run(
                full_cmd,
                env=child_env,
                capture_stderr=progress is False,
                timeout=timeout,
            )

        will_load_image = self._build_will_load_image(builder, push, load, output)
        driver = None
        if will_load_image is not False:
            driver = self.inspect(builder).driver
            if will_load_image is None:
                will_load_image = driver == "docker"
        # very special_case, must be fixed https://github.com/docker/buildx/issues/420
        if will_load_image and not tags and driver == "docker-container":
            # we have no way of fetching the image because iidfile is wrong in this case.
            will_load_image = False

        if not will_load_image:
            full_cmd.append(context_path)
            run(
                full_cmd,
                env=child_env,
                capture_stderr=progress is False,
                timeout=timeout,
            )
            return

        docker_image = pywhales.components.image.cli_wrapper.ImageCLI(
            self.client_config
        )
        if driver != "docker":
            full_cmd.append(context_path)
            run(
                full_cmd,
                env=child_env,
                capture_stderr=progress is False,
                timeout=timeout,
            )
            return docker_image.inspect(tags[0])
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_dir = Path(tmp_dir)
                iidfile = tmp_dir / "id_file.txt"
                full_cmd.add_simple_arg("--iidfile", iidfile)
                full_cmd.append(context_path)
                run(
                    full_cmd,
                    env=child_env,
                    capture_stderr=progress is False,
                    timeout=timeout,
                )
                image_id = iidfile.read_text()
                return docker_image.inspect(image_id)

    def _build_will_load_image(
        self,
        builder: ValidBuilder | None,
        push: bool,
        load: bool,
        output: BuildxOptions,
    ) -> bool | None:
        if load:
            return True
        if push:
            return False
        if output != {}:
            return output.get("type") == "docker" and "dest" not in output

        return None

    @overload
    def create(
        self,
        context_or_endpoint: str | None = None,
        bootstrap: bool = False,
        buildkitd_flags: str | None = None,
        config: ValidPath | None = None,
        platforms: builtins.list[str] | None = None,
        driver: str | None = None,
        driver_options: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        use: bool = False,
        append: bool = False,
        *,
        leave: Literal[False] = False,
        node: str | None = None,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> Builder: ...

    @overload
    def create(
        self,
        context_or_endpoint: str | None = None,
        bootstrap: bool = False,
        buildkitd_flags: str | None = None,
        config: ValidPath | None = None,
        platforms: builtins.list[str] | None = None,
        driver: str | None = None,
        driver_options: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        use: bool = False,
        append: bool = False,
        *,
        leave: Literal[True],
        node: str | None = None,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> None: ...

    @overload
    def create(
        self,
        context_or_endpoint: str | None = None,
        bootstrap: bool = False,
        buildkitd_flags: str | None = None,
        config: ValidPath | None = None,
        platforms: builtins.list[str] | None = None,
        driver: str | None = None,
        driver_options: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        use: bool = False,
        append: bool = False,
        *,
        leave: bool,
        node: str | None = None,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> Builder | None: ...

    def create(
        self,
        context_or_endpoint: str | None = None,
        bootstrap: bool = False,
        buildkitd_flags: str | None = None,
        config: ValidPath | None = None,
        platforms: builtins.list[str] | None = None,
        driver: str | None = None,
        driver_options: Mapping[str, str] = MappingProxyType({}),
        name: str | None = None,
        use: bool = False,
        append: bool = False,
        *,
        leave: bool = False,
        node: str | None = None,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> Builder | None:
        """Create a builder instance or append a node to an existing builder.

        Args:
            context_or_endpoint: Docker context or endpoint for the node; `None`
                leaves endpoint selection to Buildx.
            bootstrap: Boot the builder after creation.
            buildkitd_flags: Flags for the BuildKit daemon.
            config: Local BuildKit daemon configuration file, forwarded as
                `--buildkitd-config`; `None` omits the option.
            platforms: Platforms of the form OS/architecture/variant, such as
                `platforms=["linux/amd64", "linux/arm64"]`
            driver: Driver to use, such as `"docker-container"`, `"kubernetes"`,
                or `"remote"`. Availability depends on the installed Buildx.
            driver_options: Options for the driver.
                e.g `driver_options=dict(network="host")`
            name: Builder instance name
            use: Set the current builder instance to this builder
            append: Append a node to the current builder instance, in this case
                `name` must be provided.
            leave: Remove the node selected by `node` from the builder selected
                by `name` instead of creating or changing one.
            node: Name of the node to create or modify; `None` leaves the name
                to Buildx.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`. This is a Docker
                option, not a local deadline; `None` keeps the Buildx default.
            debug: Forward Buildx `--debug` immediately after `buildx create`.
                Debug output goes to the captured stderr and the return value is
                unchanged.

        Returns:
            A builder handle using the originating client configuration. Using
            it as a context manager removes the builder on exit. `None` when
            `leave=True`, because Buildx then prints nothing to identify a
            builder.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "create"])

        full_cmd.add_flag("--debug", debug)
        full_cmd.add_flag("--bootstrap", bootstrap)
        full_cmd.add_simple_arg("--buildkitd-flags", buildkitd_flags)
        full_cmd.add_simple_arg("--buildkitd-config", config)
        full_cmd.add_flag("--append", append)
        if platforms is not None:
            full_cmd += ["--platform", ",".join(platforms)]
        full_cmd.add_simple_arg("--driver", driver)
        if driver_options != {}:
            full_cmd.add_simple_arg(
                "--driver-opt", format_dict_for_buildx(driver_options)
            )
        full_cmd.add_simple_arg("--name", name)
        full_cmd.add_flag("--use", use)
        full_cmd.add_flag("--leave", leave)
        full_cmd.add_simple_arg("--node", node)
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))

        if context_or_endpoint is not None:
            full_cmd.append(context_or_endpoint)
        output = run(full_cmd)
        if leave:
            return None
        return Builder(self.client_config, output)

    @overload
    def disk_usage(
        self,
        *,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] | None = None,
        builder: ValidBuilder | None = None,
        status_timeout: BuildxDuration | None = None,
        verbose: Literal[False] = False,
        debug: Literal[False] = False,
        timeout: float | None = None,
    ) -> builtins.list[BuildxDiskUsage]: ...

    @overload
    def disk_usage(
        self,
        *,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] | None = None,
        builder: ValidBuilder | None = None,
        status_timeout: BuildxDuration | None = None,
        verbose: Literal[True],
        debug: bool = False,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def disk_usage(
        self,
        *,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] | None = None,
        builder: ValidBuilder | None = None,
        status_timeout: BuildxDuration | None = None,
        verbose: bool = False,
        debug: Literal[True],
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def disk_usage(
        self,
        *,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] | None = None,
        builder: ValidBuilder | None = None,
        status_timeout: BuildxDuration | None = None,
        verbose: bool = False,
        debug: bool = False,
        timeout: float | None = None,
    ) -> builtins.list[BuildxDiskUsage] | str: ...

    def disk_usage(
        self,
        *,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] | None = None,
        builder: ValidBuilder | None = None,
        status_timeout: BuildxDuration | None = None,
        verbose: bool = False,
        debug: bool = False,
        timeout: float | None = None,
    ) -> builtins.list[BuildxDiskUsage] | str:
        """Report build-cache disk usage of the selected builder.

        The default reads one JSON record per cache entry. `verbose=True` and
        `debug=True` return text instead, never parsed and never cached.

        Args:
            filters: Filters, either as a mapping or as an iterable of
                ``(key, value)`` pairs that keeps order and repeated keys.
                `None` applies none. An iterator is read once.
            builder: The builder to inspect; `None` uses the current builder.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`; `None` keeps the
                Buildx default.
            verbose: Return the multi-line `--verbose` report text instead of
                parsed records.
            debug: Forward Buildx `--debug` immediately after `buildx du` and
                return text: stdout, a newline, then the captured stderr debug
                log, in that order only. Stream interleaving is not preserved.
                Unless `verbose` is set, the default table is returned because
                the internal JSON format is dropped.
            timeout: Positive finite seconds for the local CLI, or `None` to
                inherit the client default. This is separate from
                `status_timeout`.

        Returns:
            One record per cache entry by default; otherwise the report text
            described by `verbose` and `debug`.

        Raises:
            ValueError: `timeout` is Boolean, nonpositive, or nonfinite. Checked
                before filters are consumed.
            TypeError: `filters` is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings.
            ParsingError: A record is not valid JSON or has an unexpected shape.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        filter_args = [] if filters is None else format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "buildx", "du"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_flag("--verbose", verbose)
        if not (verbose or debug):
            full_cmd += ["--format", "json"]
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_args_iterable_or_single("--filter", filter_args)
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
        if debug:
            return _run_with_debug_log(full_cmd, timeout=timeout)
        output = run(full_cmd, timeout=timeout)
        if verbose:
            return output
        parser = JsonOutputParser("disk usage", self.client_config.raw_diagnostics)
        return [
            parser.parse(BuildxDiskUsage.model_validate_json, line)
            for line in output.splitlines()
        ]

    @overload
    def inspect(
        self,
        x: ValidBuilder | None = None,
        bootstrap: bool = False,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: Literal[False] = False,
    ) -> Builder: ...

    @overload
    def inspect(
        self,
        x: ValidBuilder | None = None,
        bootstrap: bool = False,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: Literal[True],
    ) -> str: ...

    @overload
    def inspect(
        self,
        x: ValidBuilder | None = None,
        bootstrap: bool = False,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: bool,
    ) -> Builder | str: ...

    def inspect(
        self,
        x: ValidBuilder | None = None,
        bootstrap: bool = False,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> Builder | str:
        """Return the selected builder with a snapshot from the builder listing.

        Args:
            x: If `None` (the default), returns the current builder. If a string is provided,
                the builder that has this name is returned.
            bootstrap: If set to True, ensure builder has booted before inspecting.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout` to the inspection and to
                the follow-up builder listing; `None` keeps the Buildx default.
            debug: Forward Buildx `--debug` immediately after `buildx inspect`
                and return the human-readable inspection text: stdout, a
                newline, then the captured stderr debug log. No builder
                listing follows, so no handle is returned.

        Returns:
            The builder matching the name reported by `buildx inspect`, or the
            debug text when `debug=True`.

        Raises:
            ValueError: The inspected builder is absent from the subsequent list,
                for example because it was concurrently removed.
        """
        # Sadly, docker buildx inspect has no json support,
        # so, it's ugly, but we grab the name from the first line and
        # then filter "docker buildx ls".
        full_cmd = Command([*self.docker_cmd, "buildx", "inspect"])
        full_cmd.add_flag("--debug", debug)
        if x is not None:
            full_cmd.append(x)
        full_cmd.add_flag("--bootstrap", bootstrap)
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
        if debug:
            return _run_with_debug_log(full_cmd)
        output = run(full_cmd)
        name = output.splitlines()[0].split(":")[1].strip()
        for builder in self.list(status_timeout=status_timeout):
            if builder.name == name:
                return builder
        raise ValueError(
            f"Could not find a builder with the name {name}, this should "
            f"never happen unless you are quickly creating and deleting builders. "
            f"Please open an issue at https://github.com/MaraClaw/PyWhales/issues."
        )

    @overload
    def list(
        self,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: Literal[False] = False,
    ) -> builtins.list[Builder]: ...

    @overload
    def list(
        self,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: Literal[True],
    ) -> str: ...

    @overload
    def list(
        self,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: bool,
    ) -> builtins.list[Builder] | str: ...

    def list(
        self,
        *,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> builtins.list[Builder] | str:
        """Return available builders with seeded snapshots and unspecified order.

        Args:
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`; `None` keeps the
                Buildx default.
            debug: Forward Buildx `--debug` immediately after `buildx ls` and
                return the human-readable table instead of handles: stdout, a
                newline, then the captured stderr debug log. The internal JSON
                format is dropped and no handle is created or cached.

        Returns:
            Builder handles by default, or the debug text when `debug=True`.

        Raises:
            ParsingError: If a listing line is malformed, has an unexpected
                shape, or names no builder. Safe mode withholds the payload and
                chains no cause; a client with `raw_diagnostics=True` chains the
                original error.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "ls"])
        full_cmd.add_flag("--debug", debug)
        if debug:
            full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
            return _run_with_debug_log(full_cmd)
        full_cmd += ["--format", "{{json . }}"]
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
        output = run(full_cmd)
        lines = output.splitlines()
        lines = set(lines)

        parser = JsonOutputParser("builder listing", self.client_config.raw_diagnostics)
        inspect_results = [parser.parse(_parse_builder_entry, line) for line in lines]
        result = []
        for name, inspect_result in inspect_results:
            builder = Builder(self.client_config, name, is_immutable_id=True)
            builder._set_inspect_result(inspect_result)
            result.append(builder)
        return result

    @overload
    def prune(
        self,
        all: bool = False,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] = MappingProxyType({}),
        *,
        stream_logs: Literal[True],
        max_used_space: int | str | None = None,
        min_free_space: int | str | None = None,
        reserved_space: int | str | None = None,
        verbose: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str]: ...

    @overload
    def prune(
        self,
        all: bool = False,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] = MappingProxyType({}),
        stream_logs: Literal[False] = False,
        *,
        max_used_space: int | str | None = None,
        min_free_space: int | str | None = None,
        reserved_space: int | str | None = None,
        verbose: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def prune(
        self,
        all: bool = False,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] = MappingProxyType({}),
        stream_logs: bool = False,
        *,
        max_used_space: int | str | None = None,
        min_free_space: int | str | None = None,
        reserved_space: int | str | None = None,
        verbose: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str] | None: ...

    def prune(
        self,
        all: bool = False,
        filters: Mapping[str, str] | Iterable[BuildxPruneFilter] = MappingProxyType({}),
        stream_logs: bool = False,
        *,
        max_used_space: int | str | None = None,
        min_free_space: int | str | None = None,
        reserved_space: int | str | None = None,
        verbose: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> MappedProcessStream[str] | None:
        """Remove build cache on the current builder.

        The CLI starts during the call. Close streamed output when stopping early
        or use its context manager; closing does not undo cache removal.

        Args:
            all: Remove all cache, not just dangling layers
            filters: Filters to use, either as a mapping or as an iterable of
                ``(key, value)`` pairs. For example, `filters=dict(until="24h")`
                or `filters=[("until", "24h"), ("label", "ci")]`. Pairs keep
                their order and repeated keys, and an iterator is read once.
                Repeated keys are forwarded unchanged, so the CLI reports any
                combination it rejects as a `DockerException`.
            stream_logs: Return owned decoded output instead of waiting for the
                local CLI to finish.
            max_used_space: Maximum disk space allowed to keep for cache, in
                bytes or with a unit such as `"10GB"`; `None` omits it.
            min_free_space: Target free disk space after pruning, in bytes or
                with a unit; `None` omits it.
            reserved_space: Disk space always allowed to keep for cache, in
                bytes or with a unit; `None` omits it.
            verbose: Ask Buildx for a more verbose report. Blocking calls
                discard the output; use `stream_logs` to read it.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`; `None` keeps the
                Buildx default. This is a Docker option, not a local deadline.
            debug: Forward Buildx `--debug` immediately after `buildx prune`.
                The return type is unchanged.
            timeout: Positive finite seconds from local CLI launch, replacing the
                client default; `None` inherits it. This is a local deadline only
                and is never passed to Buildx's `prune --timeout` flag; use
                `status_timeout` for that option. Timing advances while stream
                consumption pauses.

        Returns:
            An owned stream of text chunks when `stream_logs=True`; otherwise
            `None` after the CLI completes.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
            ValueError: `timeout` is not positive and finite. Checked before
                filters are consumed.
            DockerException: If the CLI rejects the command, for example because
                of repeated filter keys.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped without undoing cache removal.
        """
        validate_timeout(timeout)
        filter_args = format_filters_for_cli(filters)
        full_cmd = Command([*self.docker_cmd, "buildx", "prune"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.append("--force")
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable_or_single("--filter", filter_args)
        full_cmd.add_simple_arg("--max-used-space", max_used_space)
        full_cmd.add_simple_arg("--min-free-space", min_free_space)
        full_cmd.add_simple_arg("--reserved-space", reserved_space)
        full_cmd.add_flag("--verbose", verbose)
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
        if stream_logs:
            return stream_buildx_logs(full_cmd, timeout=timeout)
        run(full_cmd, timeout=timeout)

    def remove(
        self,
        builder: Builder | str | None = None,
        *,
        all_inactive: bool = False,
        force: bool = False,
        keep_daemon: bool = False,
        keep_state: bool = False,
        status_timeout: BuildxDuration | None = None,
        debug: bool = False,
    ) -> None:
        """Remove a builder, the current builder, or every inactive builder.

        Args:
            builder: The builder to remove. `None` removes the current builder
                unless `all_inactive` is set.
            all_inactive: Remove every inactive builder instead of a named one.
                Without `force` the CLI asks for confirmation on the inherited
                stdin. Cannot be combined with `builder`.
            force: Do not prompt for confirmation.
            keep_daemon: Keep the BuildKit daemon running.
            keep_state: Keep BuildKit state.
            status_timeout: Whole seconds or a timedelta for Buildx's builder
                status loading, forwarded as `--timeout`; `None` keeps the
                Buildx default.
            debug: Forward Buildx `--debug` immediately after `buildx rm`.

        Raises:
            ValueError: `all_inactive` is combined with a builder. Raised before
                any lookup or launch.
        """
        if all_inactive and builder is not None:
            raise ValueError("all_inactive cannot be combined with a builder.")
        full_cmd = Command([*self.docker_cmd, "buildx", "rm"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_flag("--all-inactive", all_inactive)
        full_cmd.add_flag("--force", force)
        full_cmd.add_flag("--keep-daemon", keep_daemon)
        full_cmd.add_flag("--keep-state", keep_state)
        full_cmd.add_simple_arg("--timeout", to_seconds(status_timeout))
        if builder is not None:
            full_cmd.append(builder)
        run(full_cmd)

    def stop(self, builder: ValidBuilder | None, *, debug: bool = False) -> None:
        """Stop the builder instance.

        Args:
            builder: The builder to stop. If `None`,
                the current builder is stopped.
            debug: Forward Buildx `--debug` immediately after `buildx stop`.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "stop"])
        full_cmd.add_flag("--debug", debug)
        if builder is not None:
            full_cmd.append(builder)
        run(full_cmd)

    def use(
        self,
        builder: Builder | str,
        default: bool = False,
        global_: bool = False,
        *,
        debug: bool = False,
    ) -> None:
        """Set the current builder instance.

        Args:
            builder: The builder to use
            default: Set builder as default for the current context
            global_: Builder will be used even when changing contexts
            debug: Forward Buildx `--debug` immediately after `buildx use`.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "use"])

        full_cmd.add_flag("--debug", debug)
        full_cmd.add_flag("--default", default)
        full_cmd.add_flag("--global", global_)
        full_cmd.append(builder)
        run(full_cmd)

    def version(self, *, debug: bool = False) -> str:
        """Return the Docker Buildx version text reported by the CLI.

        Args:
            debug: Forward Buildx `--debug` immediately after `buildx version`.
                Debug output goes to the captured stderr, not the return value.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "version"])
        full_cmd.add_flag("--debug", debug)
        return run(full_cmd)

    def dial_stdio(
        self,
        *,
        builder: ValidBuilder | None = None,
        platform: str | None = None,
        progress: BuildxDialProgress | None = None,
        debug: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Proxy this process's stdio streams to the builder and block until it ends.

        The CLI inherits the caller's stdin, stdout and stderr, so the call
        returns only when the builder connection or the input stream closes.
        Nothing is captured: the stream is not line-delimited, and no process
        stream wrapper is offered.

        Args:
            builder: The builder to dial; `None` uses the current builder.
            platform: Target platform, used by Buildx for node selection.
            progress: Progress mode, forwarded as `--progress`; `None` keeps the
                Buildx default.
            debug: Forward Buildx `--debug` immediately after
                `buildx dial-stdio`. Debug output goes to inherited stderr.
            timeout: Positive finite seconds from launch, or `None` to inherit
                the client default. On expiry the local CLI is killed and reaped
                and `DockerTimeout` is raised; a process the CLI started may
                outlive it.

        Raises:
            ValueError: `timeout` is Boolean, nonpositive, or nonfinite.
            DockerTimeout: The local CLI exceeds its deadline.
            DockerException: The CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "buildx", "dial-stdio"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_simple_arg("--platform", platform)
        full_cmd.add_simple_arg("--progress", progress)
        run(full_cmd, tty=True, timeout=timeout)

    def is_installed(self) -> bool:
        """Check whether successful Buildx help output identifies the plugin.

        Returns:
            Whether the help output contains `buildx`.

        Raises:
            DockerException: The CLI help command fails, including when the
                plugin is unavailable. This failure does not return `False`.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "--help"])
        help_output = run(full_cmd)
        return "buildx" in help_output


def format_dict_for_buildx(options: BuildxOptions) -> str:
    return ",".join(format_mapping_for_cli(options, separator="="))


def _run_with_debug_log(
    full_cmd: Sequence[object], *, timeout: float | None = None
) -> str:
    """Run a human-readable read and return stdout, a newline, then stderr.

    Debug logs go to stderr, so the interleaving with stdout is not preserved.
    """
    stdout, stderr = run(full_cmd, return_stderr=True, timeout=timeout)
    return f"{stdout}\n{stderr}"


def _attestation_option(
    name: str, value: bool | Mapping[str, str] | None
) -> builtins.list[str]:
    """Return a `--provenance` or `--sbom` option, or nothing when unset."""
    if isinstance(value, bool):
        return [f"{name}={str(value).lower()}"]
    if isinstance(value, Mapping):
        return [name, format_dict_for_buildx(value)]
    return []


def _build_command(
    client_config: ClientConfig,
    subcommand: Sequence[str],
    *,
    add_hosts: Mapping[str, str],
    allow: Sequence[str],
    attest: dict[str, str] | None,
    build_args: Mapping[str, str],
    build_contexts: Mapping[str, str | ValidPath],
    builder: ValidBuilder | None,
    cache: bool,
    cache_from: str | BuildxOptions | builtins.list[BuildxOptions] | None,
    cache_to: str | BuildxOptions | None,
    cgroup_parent: str | None,
    file: ValidPath | None,
    labels: Mapping[str, str],
    load: bool,
    metadata_file: ValidPath | None,
    network: str | None,
    output: BuildxOptions,
    platforms: builtins.list[str] | None,
    policies: str | Sequence[str],
    progress: BuildxProgress,
    provenance: bool | Mapping[str, str] | None,
    pull: bool,
    push: bool,
    quiet: bool,
    resources: Mapping[str, str | int],
    sbom: bool | Mapping[str, str] | None,
    secrets: str | Sequence[str],
    shm_size: int | str | None,
    ssh: str | None,
    tags: str | Sequence[str],
    target: str | None,
    ulimits: str | Sequence[str],
    call: BuildxCall | None,
    no_cache_filter: str | Sequence[str] | None,
    annotations: str | Sequence[str] | None,
    debug: bool,
) -> tuple[Command, dict[str, str]]:
    """Assemble the shared `build` and `dap build` options without launching.

    Eligible build arguments move to the returned child environment before any
    executable lookup, so strict transport rejection precedes launch. The caller
    appends the context and launches.

    Args:
        client_config: Config supplying the Docker prefix and environment policy.
        subcommand: Words after the Docker executable, such as `("buildx", "build")`.
        add_hosts: Build-host names mapped to addresses.
        allow: Extra build entitlements.
        attest: Attestation parameters.
        build_args: Build-time variables.
        build_contexts: Named additional contexts.
        builder: Builder to use.
        cache: Whether to use the cache.
        cache_from: External cache sources.
        cache_to: External cache destinations.
        cgroup_parent: Parent cgroup for `RUN` instructions.
        file: Dockerfile path.
        labels: Image labels.
        load: Request the Docker exporter.
        metadata_file: Build metadata path.
        network: Network mode for `RUN` instructions.
        output: Output destination.
        platforms: Target platforms.
        policies: Policy configurations.
        progress: Progress mode.
        provenance: Provenance attestation shorthand.
        pull: Always pull base images.
        push: Request the registry exporter.
        quiet: Suppress output and print the image ID.
        resources: Resource limits for build containers.
        sbom: SBOM attestation shorthand.
        secrets: Secret specifications.
        shm_size: Shared memory size for build containers.
        ssh: SSH agent socket or keys.
        tags: Image identifiers.
        target: Target build stage.
        ulimits: Ulimit options for build containers.
        call: Frontend method.
        no_cache_filter: Stages to rebuild without cache.
        annotations: Image annotations.
        debug: Emit `--debug` immediately after the subcommand.

    Returns:
        The command without a context path and the per-launch environment
        overrides for build arguments.
    """
    build_arguments, child_env = container_environment(build_args, client_config)
    full_cmd = Command([*client_config.docker_cmd, *subcommand])

    full_cmd.add_flag("--debug", debug)
    if progress != "auto" and isinstance(progress, str):
        full_cmd += ["--progress", progress]

    full_cmd.add_args_iterable_or_single(
        "--add-host", format_mapping_for_cli(add_hosts, separator=":")
    )
    full_cmd.add_args_iterable_or_single("--allow", allow)
    if isinstance(attest, dict):
        full_cmd.add_simple_arg("--attest", format_dict_for_buildx(attest))
    full_cmd.add_args_iterable("--build-arg", build_arguments)
    full_cmd.add_args_iterable_or_single(
        "--build-context", format_mapping_for_cli(build_contexts)
    )
    full_cmd.add_simple_arg("--builder", builder)
    full_cmd.add_args_iterable_or_single("--label", format_mapping_for_cli(labels))

    full_cmd.add_simple_arg("--ssh", ssh)

    full_cmd += _attestation_option("--provenance", provenance)
    full_cmd.add_flag("--pull", pull)
    full_cmd.add_flag("--push", push)
    full_cmd += _attestation_option("--sbom", sbom)
    full_cmd.add_flag("--load", load)
    full_cmd.add_simple_arg("--metadata-file", metadata_file)
    full_cmd.add_simple_arg("--file", file)
    full_cmd.add_simple_arg("--target", target)
    if isinstance(cache_from, list):
        for item in cache_from:
            full_cmd.add_simple_arg("--cache-from", format_dict_for_buildx(item))
    elif isinstance(cache_from, Mapping):
        full_cmd.add_simple_arg("--cache-from", format_dict_for_buildx(cache_from))
    else:
        full_cmd.add_simple_arg("--cache-from", cache_from)
    if isinstance(cache_to, Mapping):
        full_cmd.add_simple_arg("--cache-to", format_dict_for_buildx(cache_to))
    else:
        full_cmd.add_simple_arg("--cache-to", cache_to)
    full_cmd.add_args_iterable_or_single("--secret", to_list(secrets))
    if output != {}:
        full_cmd += ["--output", format_dict_for_buildx(output)]
    if platforms is not None:
        full_cmd += ["--platform", ",".join(platforms)]
    full_cmd.add_simple_arg("--network", network)
    full_cmd.add_flag("--no-cache", not cache)
    full_cmd.add_args_iterable_or_single("--tag", tags)
    full_cmd.add_simple_arg("--call", call)
    if no_cache_filter is not None:
        full_cmd.add_args_iterable_or_single("--no-cache-filter", no_cache_filter)
    if annotations is not None:
        full_cmd.add_args_iterable_or_single("--annotation", annotations)
    full_cmd.add_simple_arg("--cgroup-parent", cgroup_parent)
    full_cmd.add_args_iterable_or_single("--policy", policies)
    full_cmd.add_flag("--quiet", quiet)
    full_cmd.add_args_iterable_or_single(
        "--resource", format_mapping_for_cli(resources)
    )
    full_cmd.add_simple_arg("--shm-size", shm_size)
    full_cmd.add_args_iterable_or_single("--ulimit", ulimits)
    return full_cmd, child_env


def stream_buildx_logs(
    full_cmd: Sequence[object],
    env: Mapping[str, str] | None = None,
    *,
    timeout: float | None = None,
) -> MappedProcessStream[str]:
    """Start a Buildx CLI stream and decode output with replacement.

    Args:
        full_cmd: Complete CLI arguments, preserving the originating policy.
        env: Environment overrides; `None` adds no overrides.
        timeout: Positive finite seconds from local CLI launch, or `None` for
            no deadline. The deadline advances even when output is not consumed.

    Returns:
        An owned stream of decoded stdout and stderr chunks with source labels
        discarded. Exhaust it or use `close()` or a context manager on early exit;
        cleanup reaps the local CLI without guaranteeing daemon-side cancellation.

    Raises:
        DockerTimeout: The local CLI exceeds its deadline.
        DockerException: The local CLI exits unsuccessfully.
    """
    stream = stream_stdout_and_stderr(full_cmd, env=env, timeout=timeout)
    return MappedProcessStream(
        stream, (value.decode(errors="replace") for _, value in stream)
    )
