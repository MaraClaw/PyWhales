"""Container handles and Docker CLI operations on the selected daemon.

Foreground text, detached resource handles, and owned process streams are
distinct execution modes. Local CLI cleanup does not remove daemon resources.
"""

from __future__ import annotations

import builtins
import shlex
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import (
    Literal,
    Self,
    Unpack,
    overload,
    override,
)

import pydantic
from typing_extensions import TypedDict

import pywhales.components.image.cli_wrapper
import pywhales.components.network.cli_wrapper
import pywhales.components.volume.cli_wrapper
from pywhales._environment import container_environment
from pywhales._json import loads_compat
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ParsingError,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.container.models import (
    ContainerConfig,
    ContainerGraphDriver,
    ContainerHostConfig,
    ContainerInspectResult,
    ContainerState,
    Mount,
    NetworkSettings,
)
from pywhales.components.container.top import docker_top_fields
from pywhales.components.top import ContainerTopResult, parse_table
from pywhales.enums import ContainerStatusInput, HealthStatusInput, IsolationInput
from pywhales.exceptions import NoSuchContainer
from pywhales.utils import (
    ProcessStream,
    ValidPath,
    ValidPortMapping,
    custom_parse_object_as,
    format_cpuset,
    format_port_arg,
    format_signal_arg,
    format_time_arg,
    run,
    stream_stdout_and_stderr,
    to_list,
    to_seconds,
    validate_timeout,
)

type ContainerListFilter = (
    tuple[Literal["id"], str]
    | tuple[Literal["name"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["label!"], str]
    | tuple[Literal["exited"], int]
    | tuple[Literal["status"], ContainerStatusInput]
    | tuple[Literal["ancestor"], str]
    | tuple[Literal["before"], str]
    | tuple[Literal["since"], str]
    | tuple[Literal["volume"], str]
    | tuple[Literal["network"], str]
    | tuple[Literal["publish"], str]
    | tuple[Literal["expose"], str]
    | tuple[Literal["health"], HealthStatusInput]
    | tuple[Literal["isolation"], IsolationInput]
    | tuple[Literal["is-task"], str]
    | tuple[Literal["until"], str]
)


class ExecuteArgs(TypedDict, total=False, closed=True):
    envs: Mapping[str, str]
    env_files: ValidPath | Iterable[ValidPath]
    interactive: bool
    privileged: bool
    tty: bool
    user: str | None
    workdir: ValidPath | None
    detach_keys: str | None
    timeout: float | None


class Container(ReloadableObjectFromJson[ContainerInspectResult]):
    """Reloadable container handle retaining its originating configuration.

    Inspection-backed properties may refresh through the Docker CLI. Context
    exit stops a running container, then removes it and its anonymous volumes
    unless automatic removal is enabled. Cleanup tolerates an already missing
    container; using an existing handle as a context manager opts into cleanup.
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
        with resource_cleanup(exc_val, "Container body and cleanup both failed"):
            try:
                host_config = self.host_config
                state = self.state
                assert host_config is not None
                assert state is not None
                autoremove = host_config.auto_remove
                if state.running is True:
                    self.stop()
                if autoremove is not True:
                    self.remove(volumes=True)
            except NoSuchContainer:
                return

    @override
    def _fetch_inspect_result_json(
        self, reference: str
    ) -> dict[str, pydantic.JsonValue]:
        json_str = run([*self.docker_cmd, "container", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, pydantic.JsonValue]
    ) -> ContainerInspectResult:
        return ContainerInspectResult.model_validate(json_object)

    # ----------------------------------------------------------------
    # attributes taken from the json inspect result
    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def created(self) -> datetime | None:
        return self._get_inspect_result().created

    @property
    def path(self) -> str | None:
        return self._get_inspect_result().path

    @property
    def args(self) -> list[str] | None:
        return self._get_inspect_result().args

    @property
    def state(self) -> ContainerState | None:
        return self._get_inspect_result().state

    @property
    def image(self) -> str | None:
        return self._get_inspect_result().image

    @property
    def resolv_conf_path(self) -> str | None:
        return self._get_inspect_result().resolv_conf_path

    @property
    def hostname_path(self) -> Path | None:
        return self._get_inspect_result().hostname_path

    @property
    def hosts_path(self) -> Path | None:
        return self._get_inspect_result().hosts_path

    @property
    def log_path(self) -> Path | None:
        return self._get_inspect_result().log_path

    @property
    def node(self) -> pydantic.JsonValue:
        return self._get_inspect_result().node

    @property
    def name(self) -> str | None:
        name = self._get_inspect_result().name
        return name.removeprefix("/") if name is not None else None

    @property
    def restart_count(self) -> int | None:
        return self._get_inspect_result().restart_count

    @property
    def driver(self) -> str | None:
        return self._get_inspect_result().driver

    @property
    def platform(self) -> str | None:
        return self._get_inspect_result().platform

    @property
    def mount_label(self) -> str | None:
        return self._get_inspect_result().mount_label

    @property
    def process_label(self) -> str | None:
        return self._get_inspect_result().process_label

    @property
    def app_armor_profile(self) -> str | None:
        return self._get_inspect_result().app_armor_profile

    @property
    def exec_ids(self) -> list[str] | None:
        return self._get_inspect_result().exec_ids

    @property
    def host_config(self) -> ContainerHostConfig | None:
        return self._get_inspect_result().host_config

    @property
    def graph_driver(self) -> ContainerGraphDriver | None:
        return self._get_inspect_result().graph_driver

    @property
    def size_rw(self) -> int | None:
        return self._get_inspect_result().size_rw

    @property
    def size_root_fs(self) -> int | None:
        return self._get_inspect_result().size_root_fs

    @property
    def mounts(self) -> list[Mount] | None:
        return self._get_inspect_result().mounts

    @property
    def config(self) -> ContainerConfig | None:
        return self._get_inspect_result().config

    @property
    def network_settings(self) -> NetworkSettings | None:
        return self._get_inspect_result().network_settings

    @override
    def __repr__(self) -> str:
        return f"pywhales.Container(id='{self.id[:12]}', name='{self.name}')"

    # --------------------------------------------------------------------
    # public methods

    def attach(
        self,
        detach_keys: str | None = None,
        stdin: bool = True,
        sig_proxy: bool = True,
    ) -> None:
        """Attach local standard input, output, and error streams to a running container.

        Alias: `docker.attach(...)`

        See the [`docker.container.attach`](../sub-commands/container.md#attach) command for
        information about the arguments.
        """
        ContainerCLI(self.client_config).attach(self, detach_keys, not stdin, sig_proxy)

    def commit(
        self,
        tag: str | None = None,
        author: str | None = None,
        message: str | None = None,
        pause: bool = True,
    ) -> pywhales.components.image.cli_wrapper.Image:
        """Create a new image from the container's changes.

        Alias: `docker.commit(...)`

        See the [`docker.container.commit`](../sub-commands/container.md) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).commit(
            self, tag, author, message, pause
        )

    def copy_from(
        self,
        container_path: ValidPath,
        local_path: ValidPath,
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy a container path to the Python host's filesystem.

        Args:
            container_path: Source path inside this container.
            local_path: Destination path on the Python host, not the daemon host.
            timeout: Positive finite seconds from local copy CLI launch, or `None`
                to inherit the configured default.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local copy CLI exceeds its deadline.
        """
        return ContainerCLI(self.client_config).copy(
            (self, container_path), local_path, timeout=timeout
        )

    def copy_to(
        self,
        local_path: ValidPath,
        container_path: ValidPath,
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy a local path into this container.

        Args:
            local_path: Source path on the Python host, not the daemon host.
            container_path: Destination path inside this container.
            timeout: Positive finite seconds from local copy CLI launch, or `None`
                to inherit the configured default.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local copy CLI exceeds its deadline.
        """
        return ContainerCLI(self.client_config).copy(
            local_path, (self, container_path), timeout=timeout
        )

    def diff(self) -> dict[str, str]:
        """Return filesystem change codes keyed by paths in this container.

        See the [`docker.container.diff`](../sub-commands/container.md) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).diff(self)

    @overload
    def execute(
        self,
        command: Sequence[str],
        *,
        detach: Literal[True],
        stream: bool = False,
        **kwargs: Unpack[ExecuteArgs],
    ) -> None: ...

    @overload
    def execute(
        self,
        command: Sequence[str],
        *,
        detach: Literal[False] = False,
        stream: Literal[True],
        **kwargs: Unpack[ExecuteArgs],
    ) -> ProcessStream: ...

    @overload
    def execute(
        self,
        command: Sequence[str],
        *,
        detach: Literal[False] = False,
        stream: Literal[False] = False,
        **kwargs: Unpack[ExecuteArgs],
    ) -> str: ...

    @overload
    def execute(
        self,
        command: Sequence[str],
        detach: bool = False,
        envs: Mapping[str, str] = MappingProxyType({}),
        env_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool = False,
        privileged: bool = False,
        tty: bool = False,
        user: str | None = None,
        workdir: ValidPath | None = None,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None: ...

    def execute(
        self,
        command: Sequence[str],
        detach: bool = False,
        envs: Mapping[str, str] = MappingProxyType({}),
        env_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool = False,
        privileged: bool = False,
        tty: bool = False,
        user: str | None = None,
        workdir: ValidPath | None = None,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None:
        """Execute a command in this container.

        See the [`docker.container.execute`](../sub-commands/container.md#execute)
        command for information about the arguments.

        The CLI starts during the call. For a streamed result, use its context
        manager or `close()` on early exit; a bare loop break does not close it.
        Cleanup affects the local CLI, not the container or its command.

        Returns:
            `None` when detached, an owned `(source, bytes)` stream when streaming,
            or captured stdout otherwise. With TTY output, the returned text is
            empty because output is forwarded to the terminal.
        """
        validate_timeout(timeout)
        return ContainerCLI(self.client_config).execute(
            self,
            command,
            detach,
            envs,
            env_files,
            interactive,
            privileged,
            tty,
            user,
            workdir,
            stream,
            detach_keys,
            timeout=timeout,
        )

    def exists(self) -> bool:
        """Return whether this container's immutable ID still exists.

        Only `NoSuchContainer` becomes `False`; other CLI failures propagate.
        """
        return ContainerCLI(self.client_config).exists(self.id)

    def export(self, output: ValidPath, *, timeout: float | None = None) -> None:
        """Export this container filesystem.

        See the [`docker.container.export`](../sub-commands/container.md) command for
        information about the arguments.

        Args:
            output: The path of the output tar archive.
            timeout: Positive finite seconds from local export CLI launch, or `None`
                to inherit the configured default.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local export CLI exceeds its deadline.
        """
        return ContainerCLI(self.client_config).export(self, output, timeout=timeout)

    def kill(self, signal: int | str | None = None) -> None:
        """Kill this container.

        See the [`docker.container.kill`](../sub-commands/container.md#kill) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).kill(self, signal)

    def logs(
        self,
        *,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        until: datetime | timedelta | None = None,
        timeout: float | None = None,
    ) -> str:
        """Return collected log text for this container.

        See the [`docker.container.logs`](../sub-commands/container.md#logs) command for
        information about the arguments.

        Args:
            details: Include additional logging details.
            since: Lower log timestamp or relative time limit; `None` omits it.
            tail: Number of trailing lines; `None` keeps the CLI default of all.
            timestamps: Include timestamps in log records.
            until: Upper log timestamp or relative time limit; `None` omits it.
            timeout: Positive finite seconds for the local logs CLI, or `None`
                to inherit the client default. Inspection before launch is outside the budget.

        Returns:
            Decoded stdout and stderr records joined in arrival order.

        Raises:
            DockerTimeout: The local logs CLI exceeds its deadline and is killed
                and reaped; the container is not stopped.
        """
        return ContainerCLI(self.client_config).logs(
            self,
            details=details,
            since=since,
            tail=tail,
            timestamps=timestamps,
            until=until,
            timeout=timeout,
        )

    def pause(self) -> None:
        """Pause this container.

        See the [`docker.container.pause`](../sub-commands/container.md#pause) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).pause(self)

    def unpause(self) -> None:
        """Unpause the container.

        See the [`docker.container.unpause`](../sub-commands/container.md#unpause) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).unpause(self)

    def rename(self, new_name: str) -> None:
        """Rename this container.

        See the [`docker.container.rename`](../sub-commands/container.md#rename) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).rename(self, new_name)

    def restart(self, time: int | timedelta | None = None) -> None:
        """Restart this container.

        See the [`docker.container.restart`](../sub-commands/container.md#restart) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).restart(self, time)

    def remove(self, force: bool = False, volumes: bool = False) -> None:
        """Remove this container.

        See the [`docker.container.remove`](../sub-commands/container.md#remove) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).remove(
            self, force=force, volumes=volumes
        )

    @overload
    def start(
        self,
        attach: Literal[False] = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def start(
        self,
        attach: Literal[True],
        interactive: bool = False,
        stream: Literal[False] = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def start(
        self,
        attach: Literal[True],
        interactive: bool = False,
        *,
        stream: Literal[True],
        detach_keys: str | None = None,
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def start(
        self,
        attach: bool = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None: ...

    def start(
        self,
        attach: bool = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None:
        """Start this container.

        See the [`docker.container.start`](../sub-commands/container.md#start) command for
        information about the arguments.

        Args:
            attach: Attach stdout/stderr and forward signals.
            interactive: Attach stdin (ensure it is open).
            stream: Return an owned output stream; requires `attach=True`.
            detach_keys: Override the key sequence for detaching a container.
            timeout: Positive finite seconds from the start CLI's own launch,
                replacing the client default; `None` inherits it.

        Returns:
            `None` without attachment, captured stdout when attached without
            streaming, or an owned `(source, bytes)` stream when streaming.
            Streaming requires `attach=True`; close the eager stream on early
            exit or use its context manager to release the local CLI.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local start CLI exceeds its deadline and is
                killed and reaped; the container is not stopped.
        """
        return ContainerCLI(self.client_config).start(
            self, attach, interactive, stream, detach_keys, timeout=timeout
        )

    def stop(self, time: int | timedelta | None = None) -> None:
        """Stop this container.

        See the [`docker.container.stop`](../sub-commands/container.md#stop) command for
        information about the arguments.
        """
        return ContainerCLI(self.client_config).stop(self, time)


ContainerPath = tuple[Container | str, ValidPath]
ValidContainer = Container | str


def _health_start_interval_seconds(value: int | timedelta | None) -> int | None:
    """Validate a positive whole-second startup health interval before preflight."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        seconds, remainder = divmod(value, timedelta(seconds=1))
        if seconds > 0 and not remainder:
            return seconds
    elif type(value) is int and value > 0:
        return value
    raise ValueError("health_start_interval must be positive whole seconds")


class RunArgs(TypedDict, total=False, closed=True):
    add_hosts: Iterable[tuple[str, str]]
    blkio_weight: int | None
    blkio_weight_device: Iterable[str]
    cap_add: Iterable[str]
    cap_drop: Iterable[str]
    cgroup_parent: str | None
    cgroupns: str | None
    cidfile: ValidPath | None
    cpu_period: int | None
    cpu_quota: int | None
    cpu_rt_period: int | None
    cpu_rt_runtime: int | None
    cpu_shares: int | None
    cpus: float | None
    cpuset_cpus: list[int] | str | None
    cpuset_mems: list[int] | str | None
    devices: Iterable[str]
    device_cgroup_rules: Iterable[str]
    device_read_bps: Iterable[str]
    device_read_iops: Iterable[str]
    device_write_bps: Iterable[str]
    device_write_iops: Iterable[str]
    content_trust: bool
    dns: Iterable[str]
    dns_options: Iterable[str]
    dns_search: Iterable[str]
    domainname: str | None
    entrypoint: str | None
    envs: Mapping[str, str]
    env_files: ValidPath | Iterable[ValidPath]
    expose: int | Iterable[int]
    gpus: int | str | None
    groups_add: Iterable[str]
    healthcheck: bool
    health_cmd: str | None
    health_interval: int | timedelta | None
    health_retries: int | None
    health_start_interval: int | timedelta | None
    health_start_period: int | timedelta | None
    health_timeout: int | timedelta | None
    hostname: str | None
    init: bool
    interactive: bool
    ip: str | None
    ip6: str | None
    ipc: str | None
    isolation: str | None
    kernel_memory: int | str | None
    labels: Mapping[str, str]
    label_files: Iterable[ValidPath]
    link: Iterable[ValidContainer]
    link_local_ip: Iterable[str]
    log_driver: str | None
    log_options: Iterable[str]
    mac_address: str | None
    memory: int | str | None
    memory_reservation: int | str | None
    memory_swap: int | str | None
    memory_swappiness: int | None
    mounts: Iterable[list[str]]
    name: str | None
    networks: Iterable[pywhales.components.network.cli_wrapper.ValidNetwork]
    network_aliases: Iterable[str]
    oom_kill: bool
    oom_score_adj: int | None
    pid: str | None
    pids_limit: int | None
    platform: str | None
    privileged: bool
    publish: Iterable[ValidPortMapping]
    publish_all: bool
    pull: str
    read_only: bool
    restart: str | None
    remove: bool
    runtime: str | None
    security_options: Iterable[str]
    service_ports: bool
    shm_size: int | str | None
    sig_proxy: bool
    stop_signal: int | str | None
    stop_timeout: int | None
    storage_options: Iterable[str]
    sysctl: Mapping[str, str]
    tmpfs: Iterable[ValidPath]
    tty: bool
    ulimit: Iterable[str]
    user: str | None
    userns: str | None
    uts: str | None
    volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition]
    volume_driver: str | None
    volumes_from: Iterable[ValidContainer]
    workdir: ValidPath | None
    timeout: float | None


class ContainerCLI(DockerCLICaller):
    """Container commands using the selected Docker daemon and client policy."""

    def attach(
        self,
        container: ValidContainer,
        detach_keys: str | None = None,
        stdin: bool = True,
        sig_proxy: bool = True,
    ) -> None:
        """Attach the caller's terminal streams to a running container.

        Alias: `docker.attach(...)`

        Args:
            container: The running container to attach to
            detach_keys: Override the key sequence for detaching a container
            stdin: Attach STDIN
            sig_proxy: Proxy all received signals to the process (default true)

        Raises:
            NoSuchContainer: The container does not exist during inspection or
                attachment.
        """
        self.inspect(container)

        full_cmd = Command([*self.docker_cmd, "attach"])
        full_cmd.add_simple_arg("--detach-keys", detach_keys)
        full_cmd.add_flag("--no-stdin", not stdin)
        full_cmd.add_flag("--sig-proxy", sig_proxy)
        full_cmd.append(container)

        run(full_cmd, tty=True)

    def commit(
        self,
        container: ValidContainer,
        tag: str | None = None,
        author: str | None = None,
        message: str | None = None,
        pause: bool = True,
    ) -> pywhales.components.image.cli_wrapper.Image:
        """Create a new image from a container's changes.

        Args:
            container: The container to create the image from
            tag: tag to apply on the image produced
            author: Author (e.g., "John Hannibal Smith <hannibal@a-team.com>")
            message: Commit message
            pause: Accepted but currently not forwarded; Docker's pause default
                applies regardless of this value.

        Returns:
            The committed image using the originating client configuration.
        """
        full_cmd = Command([*self.docker_cmd, "container", "commit"])

        full_cmd.add_simple_arg("--author", author)
        full_cmd.add_simple_arg("--message", message)

        # TODO: fixme
        # full_cmd += ["--pause", str(pause).lower()]

        full_cmd.append(str(container))
        if tag is not None:
            full_cmd.append(tag)

        return pywhales.components.image.cli_wrapper.Image(
            self.client_config, run(full_cmd), is_immutable_id=True
        )

    def copy(
        self,
        source: ValidPath | ContainerPath,
        destination: ValidPath | ContainerPath,
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy files or folders between a container and the local filesystem.

        Alias: `docker.copy(...)`

        Byte iterators are not supported. The call waits for the local copy CLI.

        Args:
            source: Local source path on the Python host, or a
                `(container_reference, path_in_container)` endpoint.
            destination: Local destination path on the Python host, or a
                `(container_reference, path_in_container)` endpoint. Docker
                requires exactly one container endpoint.
            timeout: Positive finite seconds from local copy CLI launch, or `None`
                to inherit the configured default. Validated before the CLI is
                resolved or launched.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local copy CLI exceeds its deadline and is killed
                and reaped.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "container", "cp"])

        if isinstance(source, tuple):
            source = f"{source[0]!s}:{source[1]}"
        else:
            source = str(source)

        if isinstance(destination, tuple):
            destination = f"{destination[0]!s}:{destination[1]}"
        else:
            destination = str(destination)

        run([*full_cmd, source, destination], timeout=timeout)

    def create(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = (),
        *,
        add_hosts: Iterable[tuple[str, str]] = (),
        blkio_weight: int | None = None,
        blkio_weight_device: Iterable[str] = (),
        cap_add: Iterable[str] = (),
        cap_drop: Iterable[str] = (),
        cgroup_parent: str | None = None,
        cgroupns: str | None = None,
        cidfile: ValidPath | None = None,
        cpu_period: int | None = None,
        cpu_quota: int | None = None,
        cpu_rt_period: int | None = None,
        cpu_rt_runtime: int | None = None,
        cpu_shares: int | None = None,
        cpus: float | None = None,
        cpuset_cpus: builtins.list[int] | str | None = None,
        cpuset_mems: builtins.list[int] | str | None = None,
        detach: bool = False,
        devices: Iterable[str] = (),
        device_cgroup_rules: Iterable[str] = (),
        device_read_bps: Iterable[str] = (),
        device_read_iops: Iterable[str] = (),
        device_write_bps: Iterable[str] = (),
        device_write_iops: Iterable[str] = (),
        content_trust: bool = False,
        dns: Iterable[str] = (),
        dns_options: Iterable[str] = (),
        dns_search: Iterable[str] = (),
        domainname: str | None = None,
        entrypoint: str | None = None,
        envs: Mapping[str, str] = {},
        env_files: ValidPath | Iterable[ValidPath] = (),
        expose: int | Iterable[int] = (),
        gpus: int | str | None = None,
        groups_add: Iterable[str] = (),
        healthcheck: bool = True,
        health_cmd: str | None = None,
        health_interval: int | timedelta | None = None,
        health_retries: int | None = None,
        health_start_interval: int | timedelta | None = None,
        health_start_period: int | timedelta | None = None,
        health_timeout: int | timedelta | None = None,
        hostname: str | None = None,
        init: bool = False,
        interactive: bool = False,
        ip: str | None = None,
        ip6: str | None = None,
        ipc: str | None = None,
        isolation: str | None = None,
        kernel_memory: int | str | None = None,
        labels: Mapping[str, str] = {},
        label_files: Iterable[ValidPath] = (),
        link: Iterable[ValidContainer] = (),
        link_local_ip: Iterable[str] = (),
        log_driver: str | None = None,
        log_options: Iterable[str] = (),
        mac_address: str | None = None,
        memory: int | str | None = None,
        memory_reservation: int | str | None = None,
        memory_swap: int | str | None = None,
        memory_swappiness: int | None = None,
        mounts: Iterable[Iterable[str]] = (),
        name: str | None = None,
        networks: Iterable[pywhales.components.network.cli_wrapper.ValidNetwork] = (),
        network_aliases: Iterable[str] = (),
        oom_kill: bool = True,
        oom_score_adj: int | None = None,
        pid: str | None = None,
        pids_limit: int | None = None,
        platform: str | None = None,
        privileged: bool = False,
        publish: Iterable[ValidPortMapping] = (),
        publish_all: bool = False,
        pull: str = "missing",
        read_only: bool = False,
        restart: str | None = None,
        remove: bool = False,
        runtime: str | None = None,
        security_options: Iterable[str] = (),
        shm_size: int | str | None = None,
        sig_proxy: bool = True,
        stop_signal: int | str | None = None,
        stop_timeout: int | None = None,
        storage_options: Iterable[str] = (),
        sysctl: Mapping[str, str] = {},
        tmpfs: Iterable[ValidPath] = (),
        tty: bool = False,
        ulimit: Iterable[str] = (),
        user: str | None = None,
        userns: str | None = None,
        uts: str | None = None,
        volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition] = (),
        volume_driver: str | None = None,
        volumes_from: Iterable[ValidContainer] = (),
        workdir: ValidPath | None = None,
    ) -> Container:
        """Create a container without starting it.

        Alias: `docker.create(...)`

        Start the returned handle with `.start()`. Image acquisition for
        `pull="missing"` or `"always"` occurs before the create CLI. The shared
        creation options follow [`docker.run`](#run). Although accepted by this
        signature, `detach=True` is forwarded to the create CLI rather than
        providing a separate return mode.

        `health_start_interval` accepts positive whole seconds as an integer or
        integral-second timedelta. The operator must qualify Docker API 1.44+
        support. Omission leaves the image's health settings unchanged.

        Returns:
            The created container using the originating client configuration.
            Using it as a context manager opts into stopping and removal on exit.

        Raises:
            TypeError: `command` is a shell string rather than an argv sequence.
            ValueError: `health_start_interval` is not positive whole seconds.
            DockerException: Image acquisition or container creation fails.
        """
        start_interval_seconds = _health_start_interval_seconds(health_start_interval)
        if isinstance(command, str):
            error_message = textwrap.dedent(
                f"""\
                When calling docker.create(), the second argument ('command') should be a sequence of strings.
                Here are some examples:
                 docker.create('ubuntu', ['ls'])
                 docker.create('ubuntu', ['cat', '/some/file.txt'])
                You can try docker.create('{image}', {shlex.split(command)}, ...).
                """
            )
            raise TypeError(error_message)

        image_cli = pywhales.components.image.cli_wrapper.ImageCLI(self.client_config)
        if pull == "missing":
            image_cli._pull_if_necessary(image)
        elif pull == "always":
            image_cli.pull(str(image))

        full_cmd = Command([*self.docker_cmd, "create"])

        full_cmd.add_args_iterable(
            "--add-host", (f"{host}:{ip}" for host, ip in add_hosts)
        )

        full_cmd.add_simple_arg("--blkio-weight", blkio_weight)
        full_cmd.add_args_iterable("--blkio-weight-device", blkio_weight_device)

        full_cmd.add_args_iterable("--cap-add", cap_add)
        full_cmd.add_args_iterable("--cap-drop", cap_drop)

        full_cmd.add_simple_arg("--cgroup-parent", cgroup_parent)
        full_cmd.add_simple_arg("--cgroupns", cgroupns)
        full_cmd.add_simple_arg("--cidfile", cidfile)

        full_cmd.add_simple_arg("--cpu-period", cpu_period)
        full_cmd.add_simple_arg("--cpu-quota", cpu_quota)
        full_cmd.add_simple_arg("--cpu-rt-period", cpu_rt_period)
        full_cmd.add_simple_arg("--cpu-rt-runtime", cpu_rt_runtime)
        full_cmd.add_simple_arg("--cpu-shares", cpu_shares)
        full_cmd.add_simple_arg("--cpus", cpus)
        full_cmd.add_simple_arg("--cpuset-cpus", format_cpuset(cpuset_cpus))
        full_cmd.add_simple_arg("--cpuset-mems", format_cpuset(cpuset_mems))

        full_cmd.add_flag("--detach", detach)

        full_cmd.add_args_iterable("--device", devices)
        full_cmd.add_args_iterable("--device-cgroup-rule", device_cgroup_rules)
        full_cmd.add_args_iterable("--device-read-bps", device_read_bps)
        full_cmd.add_args_iterable("--device-read-iops", device_read_iops)
        full_cmd.add_args_iterable("--device-write-bps", device_write_bps)
        full_cmd.add_args_iterable("--device-write-iops", device_write_iops)

        if content_trust:
            full_cmd += ["--disable-content-trust", "false"]

        full_cmd.add_args_iterable("--dns", dns)
        full_cmd.add_args_iterable("--dns-option", dns_options)
        full_cmd.add_args_iterable("--dns-search", dns_search)
        full_cmd.add_simple_arg("--domainname", domainname)

        full_cmd.add_simple_arg("--entrypoint", entrypoint)

        env_arguments, child_env = container_environment(envs, self.client_config)
        full_cmd.add_args_iterable("--env", env_arguments)
        full_cmd.add_args_iterable_or_single("--env-file", env_files)

        full_cmd.add_args_iterable_or_single("--expose", expose)

        full_cmd.add_simple_arg("--gpus", gpus)

        full_cmd.add_args_iterable("--group-add", groups_add)

        full_cmd.add_flag("--no-healthcheck", not healthcheck)
        full_cmd.add_simple_arg("--health-cmd", health_cmd)
        full_cmd.add_simple_arg("--health-interval", to_seconds(health_interval))
        full_cmd.add_simple_arg("--health-retries", health_retries)
        full_cmd.add_simple_arg(
            "--health-start-interval", to_seconds(start_interval_seconds)
        )
        full_cmd.add_simple_arg(
            "--health-start-period", to_seconds(health_start_period)
        )
        full_cmd.add_simple_arg("--health-timeout", to_seconds(health_timeout))

        full_cmd.add_simple_arg("--hostname", hostname)

        full_cmd.add_flag("--init", init)
        full_cmd.add_flag("--interactive", interactive)

        full_cmd.add_simple_arg("--ip", ip)
        full_cmd.add_simple_arg("--ip6", ip6)
        full_cmd.add_simple_arg("--ipc", ipc)

        full_cmd.add_simple_arg("--isolation", isolation)
        full_cmd.add_simple_arg("--kernel-memory", kernel_memory)

        full_cmd.add_args_mapping("--label", labels)
        full_cmd.add_args_iterable("--label-file", label_files)

        full_cmd.add_args_iterable("--link", link)
        full_cmd.add_args_iterable("--link-local-ip", link_local_ip)

        full_cmd.add_simple_arg("--log-driver", log_driver)
        full_cmd.add_args_iterable("--log-opt", log_options)

        full_cmd.add_simple_arg("--mac-address", mac_address)

        full_cmd.add_simple_arg("--memory", memory)
        full_cmd.add_simple_arg("--memory-reservation", memory_reservation)
        full_cmd.add_simple_arg("--memory-swap", memory_swap)
        full_cmd.add_simple_arg("--memory-swappiness", memory_swappiness)

        full_cmd.add_args_iterable("--mount", (",".join(x) for x in mounts))
        full_cmd.add_simple_arg("--name", name)

        full_cmd.add_args_iterable("--network", networks)
        full_cmd.add_args_iterable("--network-alias", network_aliases)

        full_cmd.add_flag("--oom-kill-disable", not oom_kill)
        full_cmd.add_simple_arg("--oom-score-adj", oom_score_adj)

        full_cmd.add_simple_arg("--pid", pid)
        full_cmd.add_simple_arg("--pids-limit", pids_limit)

        full_cmd.add_simple_arg("--platform", platform)
        full_cmd.add_flag("--privileged", privileged)

        full_cmd.add_args_iterable("-p", (format_port_arg(p) for p in publish))
        full_cmd.add_flag("--publish-all", publish_all)

        if pull == "never":
            full_cmd.add_simple_arg("--pull", "never")

        full_cmd.add_flag("--read-only", read_only)
        full_cmd.add_simple_arg("--restart", restart)
        full_cmd.add_flag("--rm", remove)

        full_cmd.add_simple_arg("--runtime", runtime)
        full_cmd.add_args_iterable("--security-opt", security_options)

        full_cmd.add_simple_arg("--shm-size", shm_size)
        if sig_proxy is False:
            full_cmd += ["--sig-proxy", "false"]

        full_cmd.add_simple_arg("--stop-signal", format_signal_arg(stop_signal))
        full_cmd.add_simple_arg("--stop-timeout", stop_timeout)

        full_cmd.add_args_iterable("--storage-opt", storage_options)
        full_cmd.add_args_mapping("--sysctl", sysctl)
        full_cmd.add_args_iterable("--tmpfs", tmpfs)
        full_cmd.add_flag("--tty", tty)
        full_cmd.add_args_iterable("--ulimit", ulimit)

        full_cmd.add_simple_arg("--user", user)
        full_cmd.add_simple_arg("--userns", userns)
        full_cmd.add_simple_arg("--uts", uts)

        for volume_definition in volumes:
            volume_definition = tuple(str(x) for x in volume_definition)
            full_cmd += ["--volume", ":".join(volume_definition)]
        full_cmd.add_simple_arg("--volume-driver", volume_driver)
        full_cmd.add_args_iterable("--volumes-from", volumes_from)

        full_cmd.add_simple_arg("--workdir", workdir)

        full_cmd.append(str(image))
        full_cmd.extend(command)
        return Container(
            self.client_config, run(full_cmd, env=child_env), is_immutable_id=True
        )

    def diff(self, container: ValidContainer) -> dict[str, str]:
        """List all the files modified, added or deleted since the container started.

        Alias: `docker.diff(...)`

        Args:
            container: The container to inspect

        Returns:
            Filesystem paths mapped to the change codes emitted by Docker, such
            as `A` for added, `C` for changed, or `D` for deleted.
        """
        full_cmd = Command([*self.docker_cmd, "diff", container])

        result_dict = {}
        result = run(full_cmd)
        for line in result.splitlines():
            result_dict[line[2:]] = line[0]
        return result_dict

    @overload
    def execute(
        self,
        container: ValidContainer,
        command: Sequence[str],
        *,
        detach: Literal[True],
        stream: bool = False,
        **kwargs: Unpack[ExecuteArgs],
    ) -> None: ...

    @overload
    def execute(
        self,
        container: ValidContainer,
        command: Sequence[str],
        *,
        detach: Literal[False] = False,
        stream: Literal[True],
        **kwargs: Unpack[ExecuteArgs],
    ) -> ProcessStream: ...

    @overload
    def execute(
        self,
        container: ValidContainer,
        command: Sequence[str],
        *,
        detach: Literal[False] = False,
        stream: Literal[False] = False,
        **kwargs: Unpack[ExecuteArgs],
    ) -> str: ...

    @overload
    def execute(
        self,
        container: ValidContainer,
        command: Sequence[str],
        detach: bool = False,
        envs: Mapping[str, str] = MappingProxyType({}),
        env_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool = False,
        privileged: bool = False,
        tty: bool = False,
        user: str | None = None,
        workdir: ValidPath | None = None,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None: ...

    def execute(
        self,
        container: ValidContainer,
        command: Sequence[str],
        detach: bool = False,
        envs: Mapping[str, str] = {},
        env_files: ValidPath | Iterable[ValidPath] = (),
        interactive: bool = False,
        privileged: bool = False,
        tty: bool = False,
        user: str | None = None,
        workdir: ValidPath | None = None,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None:
        """Execute a command inside an existing container.

        Alias: `docker.execute(...)`

        The local CLI starts during the call. Exhaust streamed output, use its
        context manager, or call `close()` on early exit. Closing or timing out
        the CLI does not guarantee termination of the container-side command.

        Args:
            container: The container to execute the command in.
            command: Command argv sequence; a shell-command string is rejected.
            detach: Return `None` after the detached local CLI completes, without
                waiting for the container-side command to finish.
            envs: Set environment variables
            env_files: Read one or more files of environment variables
            interactive: Leave stdin open during the duration of the process
                to allow communication with the parent process.
                Can be combined with `stream=True` to get an
                `ProcessStream` that allows writing to stdin while
                iterating over stdout/stderr.
            privileged: Give extended privileges to the container.
            tty: Allocate a pseudo-TTY. Allow the process to access your terminal
                to write on it.
            user: Username or UID, format: `"<name|uid>[:<group|gid>]"`
            workdir: Working directory inside the container
            stream: Return an owned stream of `("stdout", bytes)` and
                `("stderr", bytes)` records. Incompatible with TTY or detach.
            detach_keys: Override the key sequence for detaching a container.
            timeout: Positive finite seconds from local exec CLI launch, or
                `None` to inherit the client default. A stream's deadline advances even before
                or between iterations.

        Returns:
            `None` when `detach=True`, an eager owned stream when `stream=True`,
            or captured stdout otherwise. With TTY output, the returned string
            is empty because output is forwarded to the terminal.

        Raises:
            TypeError: `command` is a string rather than an argv sequence.
            ValueError: Streaming conflicts with TTY or detach, or `timeout` is
                not positive and finite.
            NoSuchContainer: The selected container does not exist.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped, without guaranteeing container-command cancellation.
            DockerException: The local exec CLI exits unsuccessfully.
        """
        validate_timeout(timeout)
        if isinstance(command, str):
            container_name = container if isinstance(container, str) else container.name
            error_message = textwrap.dedent(
                f"""\
                When calling docker.execute(), the second argument ('command') should be a sequence of strings.
                Here are some examples:
                  docker.execute('somecontainer', ['ls'])
                  docker.execute('somecontainer', ['cat', '/some/file.txt'])
                You can try docker.execute('{container_name}', {shlex.split(command)}, ...).
                """
            )
            raise TypeError(error_message)

        full_cmd = Command([*self.docker_cmd, "exec"])

        full_cmd.add_flag("--detach", detach)
        full_cmd.add_simple_arg("--detach-keys", detach_keys)

        env_arguments, child_env = container_environment(envs, self.client_config)
        full_cmd.add_args_iterable("--env", env_arguments)
        full_cmd.add_args_iterable_or_single("--env-file", env_files)

        if tty and stream:
            raise ValueError(
                "You can't set tty=True and stream=True at the same"
                "time. Their purpose are not compatible."
            )

        if detach and stream:
            raise ValueError(
                "You can't detach and stream at the same time. It's not compatible."
            )

        full_cmd.add_flag("--interactive", interactive)
        full_cmd.add_flag("--privileged", privileged)
        full_cmd.add_flag("--tty", tty)

        full_cmd.add_simple_arg("--user", user)
        full_cmd.add_simple_arg("--workdir", workdir)

        full_cmd.append(container)
        full_cmd.extend(command)

        if stream:
            return stream_stdout_and_stderr(
                full_cmd,
                env=child_env,
                pipe_stdin=interactive,
                pass_fds=(),
                timeout=timeout,
                workload_status=True,
            )
        else:
            result = run(
                full_cmd,
                env=child_env,
                tty=tty,
                pass_fds=(),
                timeout=timeout,
                workload_status=not detach,
            )
            if detach:
                return None
            else:
                return result

    def exists(self, x: ValidContainer) -> bool:
        """Return whether a container can be inspected in the selected daemon.

        Args:
            x: Container name, ID, or handle to inspect.

        Returns:
            `False` only for `NoSuchContainer`, and `True` on success. Other
            inspection failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchContainer:
            return False
        else:
            return True

    def export(
        self,
        container: ValidContainer,
        output: ValidPath,
        *,
        timeout: float | None = None,
    ) -> None:
        """Export a container's filesystem to a local tar archive.

        Alias: `docker.export(...)`

        Args:
            container: The container to export.
            output: The path of the output tar archive. Returning a generator of bytes
                is not yet implemented.
            timeout: Positive finite seconds from local export CLI launch, or `None`
                to inherit the configured default. Validated before the CLI is
                resolved or launched.

        Raises:
            ValueError: `timeout` is not positive and finite.
            NoSuchContainer: The selected container does not exist.
            DockerTimeout: The local export CLI exceeds its deadline and is killed
                and reaped.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "container", "export"])
        full_cmd.add_simple_arg("--output", output)

        full_cmd.append(container)

        if output is None:
            # we must return a generator of bytes
            raise NotImplementedError
        else:
            run(full_cmd, timeout=timeout)

    @overload
    def inspect(self, x: ValidContainer, /) -> Container: ...

    @overload
    def inspect(self, x: Iterable[ValidContainer], /) -> builtins.list[Container]: ...

    def inspect(
        self, x: ValidContainer | Iterable[ValidContainer], /
    ) -> Container | builtins.list[Container]:
        """Inspect container references and return handles with stable IDs.

        Iterable input uses one CLI invocation and validates all snapshots before
        returning handles with seeded inspection caches. Empty input skips the CLI.

        Args:
            x: A container name, ID, or handle, or an iterable of those references.

        Returns:
            One handle for scalar input, or a list in request order for iterable
            input. Duplicates are preserved as separate handles; empty input
            returns `[]`. Handles retain the originating client configuration.

        Raises:
            NoSuchContainer: A requested container does not exist.
            ParsingError: A batch snapshot is malformed or does not match its
                requested container name or ID prefix.
        """

        if isinstance(x, str | Container):
            return Container(self.client_config, str(x))
        references = [str(reference) for reference in x]
        if not references:
            return []
        json_str = run([*self.docker_cmd, "container", "inspect", *references])
        parser = Container(self.client_config, references[0], is_immutable_id=True)
        snapshots = parser._parse_inspect_results(json_str, references)
        containers = []
        for reference, snapshot in zip(references, snapshots, strict=True):
            if snapshot.id is None or not (
                reference
                and (
                    snapshot.id.startswith(reference)
                    or reference.removeprefix("/")
                    == (snapshot.name or "").removeprefix("/")
                )
            ):
                raise ParsingError(
                    f"Container inspect identity does not match {reference!r}"
                    if self.client_config.raw_diagnostics
                    else "Container inspect identity does not match the requested reference"
                )
            container = Container(self.client_config, snapshot.id, is_immutable_id=True)
            container._set_inspect_result(snapshot)
            containers.append(container)
        return containers

    def kill(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        signal: int | str | None = None,
    ) -> None:
        """Kill one or more containers.

        Alias: `docker.kill(...)`

        Args:
            containers: One or more containers to kill
            signal: The signal to send the container

        Raises:
            NoSuchContainer: A selected container does not exist.
        """
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if container_list == []:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "container", "kill"])

        full_cmd.add_simple_arg("--signal", format_signal_arg(signal))
        full_cmd += container_list

        run(full_cmd)

    @overload
    def logs(
        self,
        container: ValidContainer,
        *,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        until: datetime | timedelta | None = None,
        follow: bool = False,
        stream: Literal[False] = False,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def logs(
        self,
        container: ValidContainer,
        *,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        until: datetime | timedelta | None = None,
        follow: bool = False,
        stream: Literal[True],
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def logs(
        self,
        container: ValidContainer,
        *,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        until: datetime | timedelta | None = None,
        follow: bool = False,
        stream: bool = False,
        timeout: float | None = None,
    ) -> str | ProcessStream: ...

    def logs(
        self,
        container: ValidContainer,
        *,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        until: datetime | timedelta | None = None,
        follow: bool = False,
        stream: bool = False,
        timeout: float | None = None,
    ) -> str | ProcessStream:
        """Collect or stream a container's log output.

        Alias: `docker.logs(...)`

        Inspection precedes the logs CLI and is outside its timeout budget. The
        logs CLI starts before the call returns. Exhaust a returned stream or use
        its context manager or `close()` on early exit; a bare loop break does not
        release it. Cleanup and timeout affect only the local CLI, not the container.

        Args:
            container: The container to get the logs of
            details: Show extra details provided to logs
            since: Use a datetime or timedelta to specify the lower
                date limit for the logs.
            tail: Number of lines to show from the end of the logs (default all)
            timestamps: Put timestamps next to lines.
            until: Use a datetime or a timedelta to specify the upper date
                limit for the logs.
            follow: If `False` (the default), the logs returned are the logs up to the time
                of the function call. If `True`, the logs of the container up to the time the
                container stopped are displayed. Which means that if the container isn't stopped
                yet, the function will continue until the container is stopped.
                Which is why it is advised to use the `stream` option if you use the `follow` option.
                Without `stream`, only a `str` will be returned, possibly much later in the
                future. With `stream`, you'll be able to read the logs in real time.
            stream: Return an owned output stream instead of collecting text.
                Records are `("stdout", bytes)` and `("stderr", bytes)` pairs.
            timeout: Positive finite seconds from local logs CLI launch, or `None`
                to inherit the client default. Timing advances while consumption pauses.
                Inspection runs outside this deadline. Expiry raises `DockerTimeout`
                without stopping the container or changing post-exit pipe draining.

        Returns:
            Decoded stdout and stderr joined in arrival order when `stream=False`,
            or an eager owned stream of `(source, bytes)` records otherwise.
            Following without streaming can block until the logs CLI ends.

        Raises:
            ValueError: `timeout` is not positive and finite.
            NoSuchContainer: The selected container does not exist.
            DockerTimeout: The local logs CLI exceeds its deadline and is killed
                and reaped without stopping the container.
            DockerException: The logs CLI exits unsuccessfully.
        """

        validate_timeout(timeout)
        # first we verify that the container exists and raise an exception if not.
        self.inspect(container)

        full_cmd = Command([*self.docker_cmd, "container", "logs"])
        full_cmd.add_flag("--details", details)
        full_cmd.add_simple_arg("--since", format_time_arg(since))
        full_cmd.add_simple_arg("--tail", tail)
        full_cmd.add_flag("--timestamps", timestamps)
        full_cmd.add_simple_arg("--until", format_time_arg(until))
        full_cmd.add_flag("--follow", follow)
        full_cmd.append(container)

        iterator = stream_stdout_and_stderr(full_cmd, timeout=timeout)

        if stream:
            return iterator
        else:
            with iterator:
                return "".join(x[1].decode() for x in iterator)

    def list(
        self,
        all: bool = False,
        filters: Iterable[ContainerListFilter] = (),
    ) -> builtins.list[Container]:
        """List containers on the selected daemon, which may be remote.

        Alias: `docker.ps(...)`

        Args:
            all: If `True`, also returns containers that are not running.
            filters: Filters to apply when listing containers.

        Returns:
            Container handles in CLI order using the originating configuration.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = self.docker_cmd
        full_cmd += ["container", "list", "-q", "--no-trunc"]
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        # TODO: add a test for the fix of is_immutable_id, without it, we get
        # race conditions (we read the attributes of a container but it might not exist.
        return [
            Container(self.client_config, x, is_immutable_id=True)
            for x in run(full_cmd).splitlines()
        ]

    def pause(self, containers: ValidContainer | Iterable[ValidContainer]) -> None:
        """Pause one or more containers.

        Alias: `docker.pause(...)`

        Args:
            containers: One or more containers to pause

        Raises:
            NoSuchContainer: A selected container does not exist.
        """
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if container_list == []:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "pause"])
        full_cmd.extend(container_list)

        run(full_cmd)

    @overload
    def prune(
        self,
        filters: Iterable[ContainerListFilter] = (),
        *,
        stream_logs: Literal[True],
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def prune(
        self,
        filters: Iterable[ContainerListFilter] = (),
        stream_logs: Literal[False] = ...,
        *,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def prune(
        self,
        filters: Iterable[ContainerListFilter] = (),
        stream_logs: bool = False,
        *,
        timeout: float | None = None,
    ) -> ProcessStream | None: ...

    def prune(
        self,
        filters: Iterable[ContainerListFilter] = (),
        stream_logs: bool = False,
        *,
        timeout: float | None = None,
    ) -> ProcessStream | None:
        """Remove containers that are not running.

        Streaming starts the local CLI before returning. Exhaust the owned stream
        or close it on early exit; closing does not undo container removal.

        Args:
            filters: Filters to apply when pruning.
            stream_logs: Return an owned stdout/stderr byte stream instead of
                waiting for command completion.
            timeout: Positive finite seconds from local CLI launch; `None` inherits
                the client default. Expiry cannot undo daemon-side removal.

        Returns:
            An owned stream of `(source, bytes)` records when `stream_logs=True`;
            otherwise `None` after the local prune CLI completes.
        """
        validate_timeout(timeout)
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "container", "prune", "--force"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )
        if stream_logs:
            return stream_stdout_and_stderr(full_cmd, timeout=timeout)
        run(full_cmd, timeout=timeout)

    def rename(self, container: ValidContainer, new_name: str) -> None:
        """Change the name of a container.

        Alias: `docker.rename(...)`

        Args:
            container: The container to rename
            new_name: The new name of the container.

        Raises:
            NoSuchContainer: The selected container does not exist.
        """
        full_cmd = Command(
            [*self.docker_cmd, "container", "rename", str(container), new_name]
        )
        run(full_cmd)

    def restart(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        time: int | timedelta | None = None,
    ) -> None:
        """Restart one or more containers.

        Alias: `docker.restart(...)`

        Args:
            containers: One or more containers to restart
            time: Shutdown grace period in seconds or as a timedelta. `None`
                leaves Docker's default unchanged; not a local CLI deadline.

        Raises:
            NoSuchContainer: A selected container does not exist.
        """
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if not container_list:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "restart"])

        if time is not None:
            seconds = time.total_seconds() if isinstance(time, timedelta) else time
            full_cmd += ["--time", str(seconds)]

        full_cmd.extend(container_list)

        run(full_cmd)

    def remove(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        *,
        force: bool = False,
        volumes: bool = False,
    ) -> None:
        """Remove one or more containers.

        Alias: `docker.remove(...)`

        Args:
            containers: One or more containers.
            force: Force the removal of a running container (uses SIGKILL)
            volumes: Remove anonymous volumes associated with the container

        Raises:
            NoSuchContainer: A selected container does not exist.
        """
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if not container_list:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "container", "rm"])
        full_cmd.add_flag("--force", force)
        full_cmd.add_flag("--volumes", volumes)

        full_cmd.extend(container_list)

        run(full_cmd)

    @overload
    def run(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = ...,
        *,
        detach: Literal[True],
        stream: bool = ...,
        **kwargs: Unpack[RunArgs],
    ) -> Container: ...

    @overload
    def run(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = ...,
        *,
        detach: Literal[False] = ...,
        stream: Literal[True],
        **kwargs: Unpack[RunArgs],
    ) -> ProcessStream: ...

    @overload
    def run(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = ...,
        *,
        detach: Literal[False] = ...,
        stream: Literal[False] = ...,
        **kwargs: Unpack[RunArgs],
    ) -> str: ...

    @overload
    def run(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = (),
        *,
        detach: bool = False,
        stream: bool = False,
        **kwargs: Unpack[RunArgs],
    ) -> Container | str | ProcessStream: ...

    def run(
        self,
        image: pywhales.components.image.cli_wrapper.ValidImage,
        command: Sequence[str] = (),
        *,
        add_hosts: Iterable[tuple[str, str]] = (),
        blkio_weight: int | None = None,
        blkio_weight_device: Iterable[str] = (),
        cap_add: Iterable[str] = (),
        cap_drop: Iterable[str] = (),
        cgroup_parent: str | None = None,
        cgroupns: str | None = None,
        cidfile: ValidPath | None = None,
        cpu_period: int | None = None,
        cpu_quota: int | None = None,
        cpu_rt_period: int | None = None,
        cpu_rt_runtime: int | None = None,
        cpu_shares: int | None = None,
        cpus: float | None = None,
        cpuset_cpus: builtins.list[int] | str | None = None,
        cpuset_mems: builtins.list[int] | str | None = None,
        detach: bool = False,
        devices: Iterable[str] = (),
        device_cgroup_rules: Iterable[str] = (),
        device_read_bps: Iterable[str] = (),
        device_read_iops: Iterable[str] = (),
        device_write_bps: Iterable[str] = (),
        device_write_iops: Iterable[str] = (),
        content_trust: bool = False,
        dns: Iterable[str] = (),
        dns_options: Iterable[str] = (),
        dns_search: Iterable[str] = (),
        domainname: str | None = None,
        entrypoint: str | None = None,
        envs: Mapping[str, str] = {},
        env_files: ValidPath | Iterable[ValidPath] = (),
        expose: int | Iterable[int] = (),
        gpus: int | str | None = None,
        groups_add: Iterable[str] = (),
        healthcheck: bool = True,
        health_cmd: str | None = None,
        health_interval: int | timedelta | None = None,
        health_retries: int | None = None,
        health_start_interval: int | timedelta | None = None,
        health_start_period: int | timedelta | None = None,
        health_timeout: int | timedelta | None = None,
        hostname: str | None = None,
        init: bool = False,
        interactive: bool = False,
        ip: str | None = None,
        ip6: str | None = None,
        ipc: str | None = None,
        isolation: str | None = None,
        kernel_memory: int | str | None = None,
        labels: Mapping[str, str] = {},
        label_files: Iterable[ValidPath] = (),
        link: Iterable[ValidContainer] = (),
        link_local_ip: Iterable[str] = (),
        log_driver: str | None = None,
        log_options: Iterable[str] = (),
        mac_address: str | None = None,
        memory: int | str | None = None,
        memory_reservation: int | str | None = None,
        memory_swap: int | str | None = None,
        memory_swappiness: int | None = None,
        mounts: Iterable[builtins.list[str]] = (),
        name: str | None = None,
        networks: Iterable[pywhales.components.network.cli_wrapper.ValidNetwork] = (),
        network_aliases: Iterable[str] = (),
        oom_kill: bool = True,
        oom_score_adj: int | None = None,
        pid: str | None = None,
        pids_limit: int | None = None,
        platform: str | None = None,
        privileged: bool = False,
        publish: Iterable[ValidPortMapping] = (),
        publish_all: bool = False,
        pull: str = "missing",
        read_only: bool = False,
        restart: str | None = None,
        remove: bool = False,
        runtime: str | None = None,
        security_options: Iterable[str] = (),
        service_ports: bool = False,
        shm_size: int | str | None = None,
        sig_proxy: bool = True,
        stop_signal: int | str | None = None,
        stop_timeout: int | None = None,
        storage_options: Iterable[str] = (),
        stream: bool = False,
        sysctl: Mapping[str, str] = {},
        tmpfs: Iterable[ValidPath] = (),
        tty: bool = False,
        ulimit: Iterable[str] = (),
        user: str | None = None,
        userns: str | None = None,
        uts: str | None = None,
        volumes: Iterable[pywhales.components.volume.cli_wrapper.VolumeDefinition] = (),
        volume_driver: str | None = None,
        volumes_from: Iterable[ValidContainer] = (),
        workdir: ValidPath | None = None,
        timeout: float | None = None,
    ) -> Container | str | ProcessStream:
        """Run a container on the selected Docker daemon.

        Require the Docker CLI and a reachable daemon. Image lookup or pulling
        happens before the selected run subprocess and is outside `timeout`.
        Foreground calls block until the CLI exits. Streaming starts the local CLI
        immediately; exhaust the returned stream or use its context manager or
        `close()` when stopping early. Closing or timing out the local CLI does
        not guarantee that the daemon-side container stops or is removed.

        Args:
            image: The image to use for the container.
            command: Argument tokens after the image; empty uses its default
                command. A shell command string is not accepted.
            add_hosts: Hostname/address pairs to add to the container's hosts file,
                such as `[("build.example", "192.0.2.1")]`.
            blkio_weight: Relative block-I/O weight; None leaves Docker's default.
            blkio_weight_device: Per-device block-I/O weights in Docker syntax.
            cap_add: Linux capabilities to add.
            cap_drop: Linux capabilities to drop.
            cgroup_parent: Parent cgroup; None omits the option.
            cgroupns: Cgroup namespace mode to use, one of 'host' or 'private'.
            cidfile: Local file in which the CLI writes the container ID.
            cpu_period: Limit CPU CFS (Completely Fair Scheduler) period
            cpu_quota: Limit CPU CFS (Completely Fair Scheduler) quota
            cpu_rt_period: Limit CPU real-time period in microseconds
            cpu_rt_runtime: Limit CPU real-time runtime in microseconds
            cpu_shares: CPU shares (relative weight)
            cpus: The maximal amount of cpu the container can use.
                `1` means one cpu core.
            cpuset_cpus: Allowed CPU indices, such as `[0, 2]`, or Docker cpuset
                notation, such as `"0-3"`, `"0,2"`, or `"0-3,7"`.
            cpuset_mems: Allowed NUMA memory-node indices, using the same notation
                as `cpuset_cpus`; None omits the option.
            detach: Return a container handle without waiting for container exit.
            devices: Host device mappings in Docker syntax.
            device_cgroup_rules: Device cgroup permission rules.
            device_read_bps: Per-device read limits in bytes per second.
            device_read_iops: Per-device read limits in operations per second.
            device_write_bps: Per-device write limits in bytes per second.
            device_write_iops: Per-device write limits in operations per second.
            content_trust: Enable content-trust verification when true.
            dns: DNS server addresses.
            dns_options: Resolver options.
            dns_search: Set custom DNS search domains
            domainname: Container NIS domain name
            entrypoint: Overwrite the default ENTRYPOINT of the image
            envs: Environment variable names mapped to their values.
            env_files: Local environment files, forwarded in supplied order.
            expose: Container ports to expose without publishing them.
            gpus: GPU count or device-selection expression, such as "all" or
                "device=0,2"; requires suitable daemon-side GPU support.
            groups_add: Supplementary groups for the container process.
            healthcheck: Disable the image healthcheck when false.
            health_cmd: Healthcheck command.
            health_interval: Healthcheck interval in seconds.
            health_retries: Failures needed to mark the container unhealthy.
            health_start_interval: Positive whole-second startup probe interval,
                as an integer or integral-second timedelta. Omission preserves the
                image setting. Requires operator-qualified Docker API 1.44+.
            health_start_period: Initial healthcheck grace period in seconds.
            health_timeout: Healthcheck execution timeout in seconds.
            hostname: Container host name
            init: Run an init process inside the container.
            interactive: Leave stdin open during the duration of the process
                to allow communication with the parent process.
                Currently only works with `tty=True` for interactive use
                on the terminal.
            ip: IPv4 address (e.g., 172.30.100.104)
            ip6: IPv6 address (e.g., 2001:db8::33)
            ipc: IPC mode to use
            isolation: Container isolation technology
            kernel_memory: Kernel-memory limit in bytes or Docker size notation.
            labels: Container metadata names mapped to their values.
            label_files: Local files containing labels.
            link: Containers to link using Docker's legacy linking mechanism.
            link_local_ip: Link-local addresses.
            log_driver: Logging driver for the container
            log_options: Logging driver options in Docker syntax.
            mac_address: Container MAC address (e.g., `"92:d0:c6:0a:29:33"`)
            memory: Memory limit in bytes or Docker size notation.
            memory_reservation: Memory soft limit
            memory_swap: Swap limit equal to memory plus swap: '-1'
                to enable unlimited swap.
            memory_swappiness: Swappiness percentage; None omits the option.
            mounts: Mount option tokens joined with commas for each mount.
            name: Container name; None lets Docker choose one.
            networks: Networks to connect during creation.
            network_aliases: Network-scoped aliases.
            oom_kill: Set to `False` to disable the OOM killer for this container.
            oom_score_adj: Container OOM score adjustment.
            pid: PID namespace to use
            pids_limit: Tune container pids limit (set `-1` for unlimited)
            platform: Set platform if server is multi-platform capable.
            privileged: Give extended privileges to this container.
            publish: Ports to publish, same as the `-p` argument in the Docker CLI.
                example are `[(8000, 7000) , ("127.0.0.1:3000", 2000)]` or
                `[("127.0.0.1:3000", 2000, "udp")]`. You can also use a single entry in
                the tuple to signify that you want a random free port on the host. For example:
                `publish=[(80,)]`.
            publish_all: Publish all exposed ports to random ports.
            pull: Pull image before running ("always"|"missing"|"never") (default "missing").
            read_only: Mount the container's root filesystem as read only.
            restart: Docker restart policy; None omits the option.
            remove: Automatically remove the container when it exits.
            runtime: Runtime to use for this container.
            security_options: Security options
            service_ports: Enable service's ports to be created and mapped to the host (disabled by default)
            shm_size: Shared-memory size in bytes or Docker size notation.
            sig_proxy: Disable signal proxying when false.
            stop_signal: Signal Docker uses to stop the container.
            stop_timeout: Container shutdown grace period in seconds, distinct
                from the local CLI timeout.
            storage_options: Storage driver options for the container
            stream: Return an owned stream instead of collecting output.
            sysctl: Kernel parameter names mapped to their values.
            tmpfs: Temporary filesystem mount specifications.
            tty: Allocate a pseudo-TTY. Allow the process to access your terminal
                to write on it.
            ulimit: Resource-limit specifications in Docker syntax.
            user: Username or UID (format: `<name|uid>[:<group|gid>]`)
            userns:  User namespace to use
            uts:  UTS namespace to use
            volumes: Source, destination, and optional mode tuples. Bind-mount
                sources refer to the daemon host, which may be remote.
            volume_driver: Optional volume driver for the container
            volumes_from: Containers whose volumes should be mounted.
            workdir: The directory in the container where the process will be executed.
            timeout: Positive finite seconds from run subprocess launch, or None
                to inherit the client default. Does not budget image preflight or handle lookup.

        Returns:
            A container handle with detach=True; an owned stream of
            ("stdout" or "stderr", bytes) records with stream=True; otherwise
            captured stdout text, or an empty string when tty=True.

        Raises:
            TypeError: If command is a shell command string.
            ValueError: If timeout or health_start_interval is invalid, or detach
                and stream are both true.
            DockerTimeout: If the selected local run subprocess exceeds timeout.
            DockerException: If image preflight or the Docker command fails.
        """
        validate_timeout(timeout)
        start_interval_seconds = _health_start_interval_seconds(health_start_interval)
        if isinstance(command, str):
            error_message = textwrap.dedent(
                f"""\
                When calling docker.run(), the second argument ('command') should be a sequence of strings.
                Here are some examples:
                 docker.run('ubuntu', ['ls'])
                 docker.run('ubuntu', ['cat', '/some/file.txt'])
                You can try docker.run('{image}', {shlex.split(command)}, ...).
                """
            )
            raise TypeError(error_message)

        image_cli = pywhales.components.image.cli_wrapper.ImageCLI(self.client_config)
        if pull == "missing":
            image_cli._pull_if_necessary(image)
        elif pull == "always":
            image_cli.pull(str(image))

        full_cmd = Command([*self.docker_cmd, "container", "run"])

        full_cmd.add_args_iterable(
            "--add-host", (f"{host}:{ip}" for host, ip in add_hosts)
        )

        full_cmd.add_simple_arg("--blkio-weight", blkio_weight)
        full_cmd.add_args_iterable("--blkio-weight-device", blkio_weight_device)

        full_cmd.add_args_iterable("--cap-add", cap_add)
        full_cmd.add_args_iterable("--cap-drop", cap_drop)

        full_cmd.add_simple_arg("--cgroup-parent", cgroup_parent)
        full_cmd.add_simple_arg("--cgroupns", cgroupns)
        full_cmd.add_simple_arg("--cidfile", cidfile)

        full_cmd.add_simple_arg("--cpu-period", cpu_period)
        full_cmd.add_simple_arg("--cpu-quota", cpu_quota)
        full_cmd.add_simple_arg("--cpu-rt-period", cpu_rt_period)
        full_cmd.add_simple_arg("--cpu-rt-runtime", cpu_rt_runtime)
        full_cmd.add_simple_arg("--cpu-shares", cpu_shares)
        full_cmd.add_simple_arg("--cpus", cpus)
        full_cmd.add_simple_arg("--cpuset-cpus", format_cpuset(cpuset_cpus))
        full_cmd.add_simple_arg("--cpuset-mems", format_cpuset(cpuset_mems))

        full_cmd.add_flag("--detach", detach)

        full_cmd.add_args_iterable("--device", devices)
        full_cmd.add_args_iterable("--device-cgroup-rule", device_cgroup_rules)
        full_cmd.add_args_iterable("--device-read-bps", device_read_bps)
        full_cmd.add_args_iterable("--device-read-iops", device_read_iops)
        full_cmd.add_args_iterable("--device-write-bps", device_write_bps)
        full_cmd.add_args_iterable("--device-write-iops", device_write_iops)

        if content_trust:
            full_cmd += ["--disable-content-trust", "false"]

        full_cmd.add_args_iterable("--dns", dns)
        full_cmd.add_args_iterable("--dns-option", dns_options)
        full_cmd.add_args_iterable("--dns-search", dns_search)
        full_cmd.add_simple_arg("--domainname", domainname)

        full_cmd.add_simple_arg("--entrypoint", entrypoint)

        env_arguments, child_env = container_environment(envs, self.client_config)
        full_cmd.add_args_iterable("--env", env_arguments)
        full_cmd.add_args_iterable_or_single("--env-file", env_files)

        full_cmd.add_args_iterable_or_single("--expose", expose)

        full_cmd.add_simple_arg("--gpus", gpus)

        full_cmd.add_args_iterable_or_single("--group-add", groups_add)

        full_cmd.add_flag("--no-healthcheck", not healthcheck)
        full_cmd.add_simple_arg("--health-cmd", health_cmd)
        full_cmd.add_simple_arg("--health-interval", to_seconds(health_interval))
        full_cmd.add_simple_arg("--health-retries", health_retries)
        full_cmd.add_simple_arg(
            "--health-start-interval", to_seconds(start_interval_seconds)
        )
        full_cmd.add_simple_arg(
            "--health-start-period", to_seconds(health_start_period)
        )
        full_cmd.add_simple_arg("--health-timeout", to_seconds(health_timeout))

        full_cmd.add_simple_arg("--hostname", hostname)

        full_cmd.add_flag("--init", init)

        full_cmd.add_flag("--interactive", interactive)

        full_cmd.add_simple_arg("--ip", ip)
        full_cmd.add_simple_arg("--ip6", ip6)
        full_cmd.add_simple_arg("--ipc", ipc)

        full_cmd.add_simple_arg("--isolation", isolation)
        full_cmd.add_simple_arg("--kernel-memory", kernel_memory)

        full_cmd.add_args_mapping("--label", labels)
        full_cmd.add_args_iterable("--label-file", label_files)

        full_cmd.add_args_iterable("--link", link)
        full_cmd.add_args_iterable("--link-local-ip", link_local_ip)

        full_cmd.add_simple_arg("--log-driver", log_driver)
        full_cmd.add_args_iterable("--log-opt", log_options)

        full_cmd.add_simple_arg("--mac-address", mac_address)

        full_cmd.add_simple_arg("--memory", memory)
        full_cmd.add_simple_arg("--memory-reservation", memory_reservation)
        full_cmd.add_simple_arg("--memory-swap", memory_swap)
        full_cmd.add_simple_arg("--memory-swappiness", memory_swappiness)

        full_cmd.add_args_iterable("--mount", (",".join(x) for x in mounts))
        full_cmd.add_simple_arg("--name", name)

        full_cmd.add_args_iterable("--network", networks)
        full_cmd.add_args_iterable("--network-alias", network_aliases)

        full_cmd.add_flag("--oom-kill-disable", not oom_kill)
        full_cmd.add_simple_arg("--oom-score-adj", oom_score_adj)

        full_cmd.add_simple_arg("--pid", pid)
        full_cmd.add_simple_arg("--pids-limit", pids_limit)

        full_cmd.add_simple_arg("--platform", platform)
        full_cmd.add_flag("--privileged", privileged)

        full_cmd.add_args_iterable("-p", (format_port_arg(p) for p in publish))
        full_cmd.add_flag("--publish-all", publish_all)

        if pull == "never":
            full_cmd.add_simple_arg("--pull", "never")

        full_cmd.add_flag("--read-only", read_only)
        full_cmd.add_simple_arg("--restart", restart)
        full_cmd.add_flag("--rm", remove)

        full_cmd.add_simple_arg("--runtime", runtime)
        full_cmd.add_args_iterable("--security-opt", security_options)

        full_cmd.add_flag("--service-ports", service_ports)

        full_cmd.add_simple_arg("--shm-size", shm_size)
        if sig_proxy is False:
            full_cmd += ["--sig-proxy", "false"]

        full_cmd.add_simple_arg("--stop-signal", format_signal_arg(stop_signal))
        full_cmd.add_simple_arg("--stop-timeout", stop_timeout)

        full_cmd.add_args_iterable("--storage-opt", storage_options)
        full_cmd.add_args_mapping("--sysctl", sysctl)
        full_cmd.add_args_iterable("--tmpfs", tmpfs)
        full_cmd.add_flag("--tty", tty)
        full_cmd.add_args_iterable("--ulimit", ulimit)

        full_cmd.add_simple_arg("--user", user)
        full_cmd.add_simple_arg("--userns", userns)
        full_cmd.add_simple_arg("--uts", uts)

        for volume_definition in volumes:
            volume_definition = tuple(str(x) for x in volume_definition)
            full_cmd += ["--volume", ":".join(volume_definition)]
        full_cmd.add_simple_arg("--volume-driver", volume_driver)
        full_cmd.add_args_iterable("--volumes-from", volumes_from)

        full_cmd.add_simple_arg("--workdir", workdir)

        full_cmd.append(image)
        full_cmd.extend(command)

        if detach and stream:
            raise ValueError(
                "It's not possible to stream and detach a container at the same time."
            )

        if detach:
            return Container(
                self.client_config,
                run(full_cmd, env=child_env, pass_fds=(), timeout=timeout),
            )
        elif stream:
            return stream_stdout_and_stderr(
                full_cmd,
                env=child_env,
                pass_fds=(),
                timeout=timeout,
                workload_status=True,
            )
        else:
            return run(
                full_cmd,
                env=child_env,
                tty=tty,
                capture_stderr=False,
                pass_fds=(),
                timeout=timeout,
                workload_status=True,
            )

    @overload
    def start(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        attach: Literal[False] = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def start(
        self,
        containers: ValidContainer,
        attach: Literal[True],
        interactive: bool = False,
        stream: Literal[False] = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def start(
        self,
        containers: ValidContainer,
        attach: Literal[True],
        interactive: bool = False,
        *,
        stream: Literal[True],
        detach_keys: str | None = None,
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def start(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        attach: bool = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None: ...

    def start(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        attach: bool = False,
        interactive: bool = False,
        stream: bool = False,
        detach_keys: str | None = None,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream | None:
        """Start one or more created or stopped containers.

        Aliases: `docker.start`, `docker.container.start`,
        `pywhales.Container.start`.

        Streaming starts the local CLI immediately. Exhaust the stream or close
        it explicitly or through its context manager when stopping early. Closing
        the stream does not stop the daemon-side container.

        Args:
            containers: Containers to start; an empty iterable skips execution.
            attach: Attach stdout/stderr and forward signals.
            interactive: Attach stdin (ensure it is open).
            stream: Return an owned output stream; requires attach=True.
            detach_keys: Override the key sequence for detaching a container.
            timeout: Positive finite seconds from the start CLI's own launch,
                replacing the client default; `None` inherits it. Timing
                advances while stream consumption pauses. Validated before
                containers are consumed, even for empty input.

        Returns:
            None for empty input or unattached execution; captured text when
            attached without streaming; otherwise an owned stream of
            ("stdout" or "stderr", bytes) records.

        Raises:
            ValueError: If attaching multiple containers, streaming unattached,
                or `timeout` is not positive and finite.
            NoSuchContainer: If a selected container does not exist.
            DockerTimeout: The local start CLI exceeds its deadline and is
                killed and reaped without stopping the container.
        """
        validate_timeout(timeout)
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if container_list == []:
            # nothing to do
            return
        if attach and len(container_list) > 1:
            raise ValueError("Attaching multiple containers on start is not supported.")
        if not attach and stream:
            raise ValueError(
                "It's not possible to stream stderr and stdout if the client isn't "
                "attached to the container. Please set `attach=True`."
            )
        full_cmd = Command([*self.docker_cmd, "container", "start"])
        full_cmd.add_flag("--attach", attach)
        full_cmd.add_simple_arg("--detach-keys", detach_keys)
        full_cmd.add_flag("--interactive", interactive)
        full_cmd += container_list

        if stream:
            return stream_stdout_and_stderr(
                full_cmd, timeout=timeout, workload_status=True
            )
        elif attach:
            return run(full_cmd, timeout=timeout, workload_status=True)
        else:
            run(full_cmd, timeout=timeout)

    def stats(
        self,
        containers: ValidContainer | Iterable[ValidContainer] | None = None,
        all: bool = False,
    ) -> builtins.list[ContainerStats]:
        """Return one resource-usage snapshot for selected containers.

        Require the Docker CLI and a reachable daemon. Byte-valued measurements
        are parsed from Docker's formatted sizes; percentage values remain in
        percentage units rather than fractions.

        Args:
            containers: Containers to sample; None selects Docker's default set.
                Empty input returns [] unless all=True, which queries all.
            all: Include stopped containers as well as running containers.

        Returns:
            Parsed usage records in CLI output order.
        """
        containers_list = to_list(containers) if containers else []
        if len(containers_list) == 0 and containers is not None and not all:
            return []

        full_cmd = Command(
            [
                *self.docker_cmd,
                "container",
                "stats",
                "--format",
                "{{json .}}",
                "--no-stream",
                "--no-trunc",
            ]
        )
        full_cmd.add_flag("--all", all)
        if containers:
            full_cmd.extend(containers_list)

        stats_output = run(full_cmd)
        return [ContainerStats(loads_compat(x)) for x in stats_output.splitlines()]

    def stop(
        self,
        containers: ValidContainer | Iterable[ValidContainer],
        time: int | timedelta | None = None,
    ) -> None:
        """Stop one or more running containers.

        Alias: `docker.stop(...)`

        Aliases: `docker.stop`, `docker.container.stop`,
        `pywhales.Container.stop`.

        Args:
            containers: Containers to stop; empty input skips execution.
            time: Shutdown grace period in seconds before Docker kills the
                container; None leaves the option unset for Docker to decide.

        Raises:
            NoSuchContainer: If a selected container does not exist.
        """
        container_list = (
            [containers]
            if isinstance(containers, str | Container)
            else list(containers)
        )
        if container_list == []:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "container", "stop"])
        full_cmd.add_simple_arg(
            "--time", format_time_arg(time) if isinstance(time, timedelta) else time
        )
        full_cmd.extend(container_list)

        run(full_cmd)

    @overload
    def top(
        self,
        container: ValidContainer,
        ps_args: Sequence[str] = (),
        *,
        raw: Literal[False] = False,
    ) -> ContainerTopResult: ...

    @overload
    def top(
        self,
        container: ValidContainer,
        ps_args: Sequence[str] = (),
        *,
        raw: Literal[True],
    ) -> str: ...

    @overload
    def top(
        self, container: ValidContainer, ps_args: Sequence[str] = (), *, raw: bool
    ) -> ContainerTopResult | str: ...

    def top(
        self,
        container: ValidContainer,
        ps_args: Sequence[str] = (),
        *,
        raw: bool = False,
    ) -> ContainerTopResult | str:
        """Return a container's running processes without changing its state.

        Alias: `docker.top(...)`.

        Args:
            container: Container whose processes should be listed.
            ps_args: Docker ps option tokens, forwarded after the container.
                Defaults to the Engine's ps selection (normally -ef).
            raw: Return the output from the existing CLI execution path as text
                instead of parsing it. Required for ambiguous field selections.

        Returns:
            Unparsed CLI text when raw=True. Otherwise, original headings,
            ordered string rows, and raw_output. Supported selections have scalar
            fields and optionally one final command/free-text field.

        Raises:
            TopParseError: Unsupported or malformed parsed layout; use raw=True.
            NoSuchContainer: The container does not exist.
        """
        output = run([*self.docker_cmd, "container", "top", container, *ps_args])
        if raw:
            return output
        table = parse_table(output, docker_top_fields(ps_args))
        return ContainerTopResult(table.headings, table.rows, output)

    def unpause(self, x: ValidContainer | Iterable[ValidContainer], /) -> None:
        """Unpause all processes within one or more containers.

        Alias: `docker.unpause(...)`

        Args:
            x: Containers to unpause; empty input skips execution.

        Raises:
            NoSuchContainer: If a selected container does not exist.
        """
        containers = [x] if isinstance(x, str | Container) else list(x)
        if len(containers) == 0:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "container", "unpause"])
        full_cmd.extend(containers)
        run(full_cmd)

    def update(
        self,
        x: ValidContainer | Iterable[ValidContainer],
        /,
        *,
        blkio_weight: int | None = None,
        cpu_period: int | None = None,
        cpu_quota: int | None = None,
        cpu_rt_period: int | None = None,
        cpu_rt_runtime: int | None = None,
        cpu_shares: int | None = None,
        cpus: float | None = None,
        cpuset_cpus: str | None = None,
        cpuset_mems: str | None = None,
        kernel_memory: int | str | None = None,
        memory: int | str | None = None,
        memory_reservation: int | str | None = None,
        memory_swap: int | str | None = None,
        pids_limit: int | None = None,
        restart: str | None = None,
    ) -> None:
        """Update configuration of one or more containers.

        Alias: `docker.update(...)`

        Args:
            x: Containers to update; empty input skips execution.
            blkio_weight: Block IO (relative weight), between 10 and 1000,
                or 0 to disable (default 0)
            cpu_period: Limit CPU CFS (Completely Fair Scheduler) period
            cpu_quota: Limit CPU CFS (Completely Fair Scheduler) quota
            cpu_rt_period: Limit CPU real-time period in microseconds
            cpu_rt_runtime: Limit CPU real-time runtime in microseconds
            cpu_shares: CPU shares (relative weight)
            cpus: The maximal amount of cpu the container can use.
                `1` means one cpu core.
            cpuset_cpus: Allowed CPU indices in Docker cpuset notation, such as
                `"0-3"`, `"0,2"`, or `"0-3,7"`.
            cpuset_mems: Allowed NUMA memory nodes in the same notation as
                `cpuset_cpus`; None omits the option.
            kernel_memory: Kernel-memory limit in bytes or Docker size notation.
            memory: Memory limit in bytes or Docker size notation, such as `"43m"`.
            memory_reservation: Memory soft limit
            memory_swap: Swap limit equal to memory plus swap: '-1'
                to enable unlimited swap.
            pids_limit: Tune container pids limit (set `-1` for unlimited)
            restart: Restart policy to apply when a container exits (default "no")

        Raises:
            NoSuchContainer: If a selected container does not exist.
        """
        containers = [x] if isinstance(x, str | Container) else list(x)
        if containers == []:
            # nothing to do
            return
        full_cmd = Command([*self.docker_cmd, "container", "update"])
        full_cmd.add_simple_arg("--blkio-weight", blkio_weight)
        full_cmd.add_simple_arg("--cpu-period", cpu_period)
        full_cmd.add_simple_arg("--cpu-quota", cpu_quota)
        full_cmd.add_simple_arg("--cpu-rt-period", cpu_rt_period)
        full_cmd.add_simple_arg("--cpu-rt-runtime", cpu_rt_runtime)
        full_cmd.add_simple_arg("--cpu-shares", cpu_shares)
        full_cmd.add_simple_arg("--cpus", cpus)
        full_cmd.add_simple_arg("--cpuset-cpus", cpuset_cpus)
        full_cmd.add_simple_arg("--cpuset-mems", cpuset_mems)
        full_cmd.add_simple_arg("--kernel-memory", kernel_memory)
        full_cmd.add_simple_arg("--memory", memory)
        full_cmd.add_simple_arg("--memory-reservation", memory_reservation)
        full_cmd.add_simple_arg("--memory-swap", memory_swap)
        full_cmd.add_simple_arg("--pids-limit", pids_limit)
        full_cmd.add_simple_arg("--restart", restart)
        full_cmd.extend(containers)
        run(full_cmd)

    @overload
    def wait(self, x: ValidContainer, *, timeout: float | None = None) -> int: ...

    @overload
    def wait(
        self, x: Iterable[ValidContainer], *, timeout: float | None = None
    ) -> builtins.list[int]: ...

    def wait(
        self,
        x: ValidContainer | Iterable[ValidContainer],
        *,
        timeout: float | None = None,
    ) -> int | builtins.list[int]:
        """Block until selected containers stop and return their exit codes.

        Alias: `docker.wait(...)`


        Args:
            x: Containers to wait for; an empty iterable skips execution.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before empty input returns.

        Returns:
            One exit code for a scalar reference, or codes in Docker's output
            order for iterable input, including [] for empty input.

        Raises:
            ValueError: Timeout is boolean, nonfinite, zero or negative.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped. Waiting containers are not stopped.
            NoSuchContainer: If a selected container does not exist.
        """
        validate_timeout(timeout)
        containers = to_list(x)
        if len(containers) == 0:
            # nothing to do
            return []

        full_cmd = Command([*self.docker_cmd, "container", "wait", *containers])
        if isinstance(x, Iterable) and not isinstance(x, str):
            exit_codes = run(full_cmd, timeout=timeout)
            return [int(exit_code) for exit_code in exit_codes.splitlines()]
        else:
            return int(run(full_cmd, timeout=timeout))


class ContainerStats:
    """Parsed container usage snapshot with byte counts and percentage values."""

    def __init__(self, json_dict: dict[str, str]) -> None:
        """Parse one formatted JSON record from Docker container stats.

        Args:
            json_dict: CLI record containing formatted I/O, CPU, memory, identity,
                and network measurements.
        """
        self.block_read: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["BlockIO"].split("/")[0]
        )
        self.block_write: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["BlockIO"].split("/")[1]
        )
        self.cpu_percentage: float = float(json_dict["CPUPerc"][:-1])
        self.container: str = json_dict["Container"]
        self.container_id: str = json_dict["ID"]
        self.memory_percentage: float = float(json_dict["MemPerc"][:-1])
        self.memory_used: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["MemUsage"].split("/")[0]
        )
        self.memory_limit: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["MemUsage"].split("/")[1]
        )
        self.container_name: str = json_dict["Name"]
        self.net_upload: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["NetIO"].split("/")[0]
        )
        self.net_download: int = custom_parse_object_as(
            pydantic.ByteSize, json_dict["NetIO"].split("/")[1]
        )

    @override
    def __repr__(self) -> str:
        attr = ", ".join(f"{key}={value}" for key, value in self.__dict__.items())
        return f"<{self.__class__} object, attributes are {attr}>"
