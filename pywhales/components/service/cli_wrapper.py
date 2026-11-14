"""Swarm service administration, task discovery, and owned service-log streams."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType, TracebackType
from typing import (
    Literal,
    Self,
    assert_never,
    overload,
    override,
)

from pydantic import JsonValue

import pywhales.components.task.cli_wrapper
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components._hydration import (
    hydrate_handles,
    release_pins,
    resolve_identities,
)
from pywhales.components._swarm_inspect import parse_swarm_inspect_results
from pywhales.components.service._logs import logs_command
from pywhales.components.service._spec_options import (
    ServiceLongOption,
    ServiceModeInput,
    ServicePort,
    encode_options,
)
from pywhales.components.service._update_options import update_options
from pywhales.components.service.models import (
    ServiceEndpoint,
    ServiceInspectResult,
    ServiceSpec,
    ServiceUpdateStatus,
    ServiceVersion,
)
from pywhales.enums import (
    RestartConditionInput,
    RollbackFailureActionInput,
    UpdateFailureActionInput,
    UpdateOrderInput,
)
from pywhales.exceptions import NoSuchService
from pywhales.utils import (
    ProcessStream,
    ValidPath,
    run,
    stream_stdout_and_stderr,
    to_list,
    validate_timeout,
)

type ServiceListFilter = (
    tuple[Literal["id"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["mode"], str]
    | tuple[Literal["name"], str]
)


type ServicePsFilter = (
    tuple[Literal["id"], str]
    | tuple[Literal["name"], str]
    | tuple[Literal["node"], str]
    | tuple[Literal["desired-state"], str]
)


class Service(ReloadableObjectFromJson[ServiceInspectResult]):
    """Reloadable Swarm service handle with context-exit removal.

    Cleanup is attempted even if the body raises, and ignores a service that is
    already gone. Other cleanup failures are preserved in an exception group
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
        with resource_cleanup(exc_val, "Service body and cleanup both failed"):
            try:
                self.remove()
            except NoSuchService:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "service", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> ServiceInspectResult:
        return ServiceInspectResult.model_validate(json_object)

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def version(self) -> ServiceVersion | None:
        return self._get_inspect_result().version

    @property
    def created_at(self) -> datetime | None:
        return self._get_inspect_result().created_at

    @property
    def updated_at(self) -> datetime | None:
        return self._get_inspect_result().updated_at

    @property
    def spec(self) -> ServiceSpec | None:
        return self._get_inspect_result().spec

    @property
    def previous_spec(self) -> ServiceSpec | None:
        return self._get_inspect_result().previous_spec

    @property
    def endpoint(self) -> ServiceEndpoint | None:
        return self._get_inspect_result().endpoint

    @property
    def update_status(self) -> ServiceUpdateStatus | None:
        return self._get_inspect_result().update_status

    def ps(
        self, *, filters: Iterable[ServicePsFilter] | None = None
    ) -> list[pywhales.components.task.cli_wrapper.Task]:
        """Return task handles associated with this service.

        Args:
            filters: Docker filter pairs such as `("desired-state", "running")`
                forwarded to `docker.service.ps`, or None for no filter.
        """
        return ServiceCLI(self.client_config).ps(self, filters=filters)

    def remove(self) -> None:
        """Remove this service from the Swarm.

        A context manager attempts this removal even if its body raises.
        """
        ServiceCLI(self.client_config).remove(self)

    def scale(
        self, new_scale: int, detach: bool = False, *, timeout: float | None = None
    ) -> None:
        """Change the scale of a service.

        See the [`docker.service.scale`](../sub-commands/service.md#scale) command for
        information about the arguments, including the local `timeout` deadline.
        """
        ServiceCLI(self.client_config).scale(
            {self: new_scale}, detach=detach, timeout=timeout
        )

    def update(
        self,
        detach: bool = False,
        force: bool = False,
        image: str | None = None,
        with_registry_authentication: bool = False,
        quiet: bool = False,
        replicas: int | None = None,
        *,
        update_parallelism: int | None = None,
        update_delay: int | timedelta | None = None,
        update_failure_action: UpdateFailureActionInput | None = None,
        update_monitor: int | timedelta | None = None,
        update_max_failure_ratio: float | None = None,
        update_order: UpdateOrderInput | None = None,
        rollback_parallelism: int | None = None,
        rollback_delay: int | timedelta | None = None,
        rollback_failure_action: RollbackFailureActionInput | None = None,
        rollback_monitor: int | timedelta | None = None,
        rollback_max_failure_ratio: float | None = None,
        rollback_order: UpdateOrderInput | None = None,
        args: Sequence[str] | None = None,
        cap_add: Sequence[str] = (),
        cap_drop: Sequence[str] = (),
        config_add: Sequence[ServiceLongOption] = (),
        config_rm: Sequence[str] = (),
        constraint_add: Sequence[str] = (),
        constraint_rm: Sequence[str] = (),
        container_label_add: Mapping[str, str] = MappingProxyType({}),
        container_label_rm: Sequence[str] = (),
        credential_spec: str | None = None,
        dns_add: Sequence[str] = (),
        dns_rm: Sequence[str] = (),
        dns_option_add: Sequence[str] = (),
        dns_option_rm: Sequence[str] = (),
        dns_search_add: Sequence[str] = (),
        dns_search_rm: Sequence[str] = (),
        endpoint_mode: str | None = None,
        entrypoint: str | None = None,
        env_add: Mapping[str, str] = MappingProxyType({}),
        env_rm: Sequence[str] = (),
        generic_resource_add: Sequence[str] = (),
        generic_resource_rm: Sequence[str] = (),
        group_add: Sequence[str] = (),
        group_rm: Sequence[str] = (),
        health_cmd: str | None = None,
        health_interval: int | timedelta | None = None,
        health_retries: int | None = None,
        health_start_interval: int | timedelta | None = None,
        health_start_period: int | timedelta | None = None,
        health_timeout: int | timedelta | None = None,
        host_add: Mapping[str, str] = MappingProxyType({}),
        host_rm: Mapping[str, str] = MappingProxyType({}),
        hostname: str | None = None,
        init: bool | None = None,
        isolation: str | None = None,
        label_add: Mapping[str, str] = MappingProxyType({}),
        label_rm: Sequence[str] = (),
        limit_cpu: float | None = None,
        limit_memory: str | None = None,
        limit_pids: int | None = None,
        log_driver: str | None = None,
        log_opts: Mapping[str, str] = MappingProxyType({}),
        max_concurrent: int | None = None,
        memory_swap: int | str | None = None,
        memory_swappiness: int | None = None,
        mount_add: Sequence[ServiceLongOption] = (),
        mount_rm: Sequence[str] = (),
        network_add: Sequence[str] = (),
        network_rm: Sequence[str] = (),
        no_healthcheck: bool = False,
        no_resolve_image: bool = False,
        oom_score_adj: int | None = None,
        placement_pref_add: Sequence[str] = (),
        placement_pref_rm: Sequence[str] = (),
        publish_add: Iterable[ServicePort] = (),
        publish_rm: Iterable[ServicePort] = (),
        read_only: bool | None = None,
        replicas_max_per_node: int | None = None,
        reserve_cpu: float | None = None,
        reserve_memory: str | None = None,
        restart_condition: RestartConditionInput | None = None,
        restart_delay: int | timedelta | None = None,
        restart_max_attempts: int | None = None,
        restart_window: int | timedelta | None = None,
        rollback: bool = False,
        secret_add: Sequence[ServiceLongOption] = (),
        secret_rm: Sequence[str] = (),
        stop_grace_period: int | timedelta | None = None,
        stop_signal: str | None = None,
        sysctl_add: Mapping[str, str] = MappingProxyType({}),
        sysctl_rm: Sequence[str] = (),
        tty: bool | None = None,
        ulimit_add: Sequence[str] = (),
        ulimit_rm: Sequence[str] = (),
        user: str | None = None,
        workdir: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Update this service.

        See the [`docker.service.update`](../sub-commands/service.md#update) command for
        information about the arguments, including the local `timeout` deadline.
        """
        ServiceCLI(self.client_config).update(
            self,
            detach,
            force,
            image,
            with_registry_authentication,
            quiet,
            replicas,
            update_parallelism=update_parallelism,
            update_delay=update_delay,
            update_failure_action=update_failure_action,
            update_monitor=update_monitor,
            update_max_failure_ratio=update_max_failure_ratio,
            update_order=update_order,
            rollback_parallelism=rollback_parallelism,
            rollback_delay=rollback_delay,
            rollback_failure_action=rollback_failure_action,
            rollback_monitor=rollback_monitor,
            rollback_max_failure_ratio=rollback_max_failure_ratio,
            rollback_order=rollback_order,
            args=args,
            cap_add=cap_add,
            cap_drop=cap_drop,
            config_add=config_add,
            config_rm=config_rm,
            constraint_add=constraint_add,
            constraint_rm=constraint_rm,
            container_label_add=container_label_add,
            container_label_rm=container_label_rm,
            credential_spec=credential_spec,
            dns_add=dns_add,
            dns_rm=dns_rm,
            dns_option_add=dns_option_add,
            dns_option_rm=dns_option_rm,
            dns_search_add=dns_search_add,
            dns_search_rm=dns_search_rm,
            endpoint_mode=endpoint_mode,
            entrypoint=entrypoint,
            env_add=env_add,
            env_rm=env_rm,
            generic_resource_add=generic_resource_add,
            generic_resource_rm=generic_resource_rm,
            group_add=group_add,
            group_rm=group_rm,
            health_cmd=health_cmd,
            health_interval=health_interval,
            health_retries=health_retries,
            health_start_interval=health_start_interval,
            health_start_period=health_start_period,
            health_timeout=health_timeout,
            host_add=host_add,
            host_rm=host_rm,
            hostname=hostname,
            init=init,
            isolation=isolation,
            label_add=label_add,
            label_rm=label_rm,
            limit_cpu=limit_cpu,
            limit_memory=limit_memory,
            limit_pids=limit_pids,
            log_driver=log_driver,
            log_opts=log_opts,
            max_concurrent=max_concurrent,
            memory_swap=memory_swap,
            memory_swappiness=memory_swappiness,
            mount_add=mount_add,
            mount_rm=mount_rm,
            network_add=network_add,
            network_rm=network_rm,
            no_healthcheck=no_healthcheck,
            no_resolve_image=no_resolve_image,
            oom_score_adj=oom_score_adj,
            placement_pref_add=placement_pref_add,
            placement_pref_rm=placement_pref_rm,
            publish_add=publish_add,
            publish_rm=publish_rm,
            read_only=read_only,
            replicas_max_per_node=replicas_max_per_node,
            reserve_cpu=reserve_cpu,
            reserve_memory=reserve_memory,
            restart_condition=restart_condition,
            restart_delay=restart_delay,
            restart_max_attempts=restart_max_attempts,
            restart_window=restart_window,
            rollback=rollback,
            secret_add=secret_add,
            secret_rm=secret_rm,
            stop_grace_period=stop_grace_period,
            stop_signal=stop_signal,
            sysctl_add=sysctl_add,
            sysctl_rm=sysctl_rm,
            tty=tty,
            ulimit_add=ulimit_add,
            ulimit_rm=ulimit_rm,
            user=user,
            workdir=workdir,
            timeout=timeout,
        )

    def exists(self) -> bool:
        """Return whether this service can still be inspected in the Swarm."""
        return ServiceCLI(self.client_config).exists(self.id)


ValidService = str | Service


class ServiceCLI(DockerCLICaller):
    """Service commands requiring the configured CLI and a Swarm manager daemon."""

    def create(
        self,
        image: str,
        command: builtins.list[str] | None,
        cap_add: Sequence[str] = (),
        cap_drop: Sequence[str] = (),
        constraints: Sequence[str] = (),
        detach: bool = False,
        dns: Sequence[str] = (),
        dns_options: Sequence[str] = (),
        dns_search: Sequence[str] = (),
        endpoint_mode: str | None = None,
        entrypoint: str | None = None,
        envs: Mapping[str, str] = MappingProxyType({}),
        env_files: ValidPath | Sequence[ValidPath] = (),
        generic_resources: Sequence[str] = (),
        groups: Sequence[str] = (),
        healthcheck: bool = True,
        health_cmd: str | None = None,
        health_interval: int | timedelta | None = None,
        health_retries: int | None = None,
        health_start_period: int | timedelta | None = None,
        health_timeout: int | timedelta | None = None,
        hosts: Mapping[str, str] = MappingProxyType({}),
        hostname: str | None = None,
        init: bool = False,
        isolation: str | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        limit_cpu: float | None = None,
        limit_memory: str | None = None,
        limit_pids: int | None = None,
        log_driver: str | None = None,
        network: str | None = None,
        restart_condition: RestartConditionInput | None = None,
        restart_max_attempts: int | None = None,
        secrets: builtins.list[dict[str, str]] | None = None,
        mounts: builtins.list[dict[str, str]] | None = None,
        *,
        name: str | None = None,
        replicas: int | None = None,
        mode: ServiceModeInput | None = None,
        publish: Iterable[ServicePort] = (),
        config: Sequence[ServiceLongOption] = (),
        user: str | None = None,
        workdir: str | None = None,
        tty: bool = False,
        read_only: bool = False,
        stop_signal: str | None = None,
        stop_grace_period: int | timedelta | None = None,
        log_opts: Mapping[str, str] = MappingProxyType({}),
        sysctls: Mapping[str, str] = MappingProxyType({}),
        ulimits: Sequence[str] = (),
        reserve_cpu: float | None = None,
        reserve_memory: str | None = None,
        restart_delay: int | timedelta | None = None,
        restart_window: int | timedelta | None = None,
        placement_prefs: Sequence[str] = (),
        with_registry_auth: bool = False,
        container_labels: Mapping[str, str] = MappingProxyType({}),
        credential_spec: str | None = None,
        health_start_interval: int | timedelta | None = None,
        max_concurrent: int | None = None,
        memory_swap: int | str | None = None,
        memory_swappiness: int | None = None,
        no_resolve_image: bool = False,
        oom_score_adj: int | None = None,
        replicas_max_per_node: int | None = None,
        update_parallelism: int | None = None,
        update_delay: int | timedelta | None = None,
        update_failure_action: UpdateFailureActionInput | None = None,
        update_monitor: int | timedelta | None = None,
        update_max_failure_ratio: float | None = None,
        update_order: UpdateOrderInput | None = None,
        rollback_parallelism: int | None = None,
        rollback_delay: int | timedelta | None = None,
        rollback_failure_action: RollbackFailureActionInput | None = None,
        rollback_monitor: int | timedelta | None = None,
        rollback_max_failure_ratio: float | None = None,
        rollback_order: UpdateOrderInput | None = None,
        timeout: float | None = None,
    ) -> Service:
        """Create a Docker Swarm service.

        This requires a Swarm manager and changes daemon-managed service state.
        Unless detach is True, Docker waits for service convergence. Options after
        `mounts` are keyword-only; every value is encoded and validated before the
        CLI executable is resolved.

        Args:
            image: The image to use as the base for the service.
            command: Container command arguments, or None to use the image default.
            cap_add: Linux capabilities to add.
            cap_drop: Linux capabilities to remove.
            constraints: Scheduling constraints to apply.
            detach: Return after submission instead of waiting for convergence.
            dns: Custom DNS servers.
            dns_options: DNS resolver options.
            dns_search: DNS search domains.
            endpoint_mode: Endpoint mode, or None to omit the option.
            entrypoint: Entrypoint override, or None to use the image default.
            envs: Environment variable values for service containers.
            env_files: Local files supplying container environment variables.
            generic_resources: Generic resource requirements for scheduling.
            groups: Supplementary container groups.
            healthcheck: Disable the image's health check when False.
            health_cmd: Health-check command, or None to omit the option.
            health_interval: Interval between health checks in seconds, or None
                to omit the option.
            health_retries: Failures before marking a container unhealthy.
                None omits the option.
            health_start_period: Health-check startup grace period in seconds,
                or None to omit the option.
            health_timeout: Health-check timeout in seconds, or None to omit it.
            hosts: Host entries used to build repeated --host options.
            hostname: Container hostname, or None to omit the option.
            init: Run an init process inside service containers when True.
            isolation: Container isolation mode, or None to omit the option.
            labels: Service metadata labels.
            limit_cpu: CPU limit, or None to omit the option.
            limit_memory: Memory limit with Docker-supported units, or None to omit it.
            limit_pids: PID limit, or None to omit the option.
            log_driver: Logging driver, or None to omit the option.
            network: Network attachment specification, or None to omit it.
            restart_condition: Restart policy condition, or None to omit it.
            restart_max_attempts: Maximum restart attempts, or None to omit the option.
            secrets: Secret option mappings, or None to add no secrets.
            mounts: Mount option mappings, or None to add no mounts.
            name: Service name, or None to let Docker generate one.
            replicas: Number of tasks for a replicated mode, or None to keep the
                Docker default. Zero is sent.
            mode: Service mode, or None for Docker's default of replicated. The job
                modes run tasks to completion; `max_concurrent` applies to them.
            publish: Ports to publish as node ports. Each is a port tuple in the
                container `publish` forms or a mapping of long-form keys such as
                published, target, protocol and mode.
            config: Config references to expose, each a mapping of long-form keys
                such as source, target, uid, gid and mode.
            user: Username or UID, optionally followed by a group, or None to omit.
            workdir: Working directory inside the container, or None to omit it.
            tty: Allocate a pseudo-TTY when True.
            read_only: Mount the container root filesystem read-only when True.
            stop_signal: Signal that stops the container, or None to omit it.
            stop_grace_period: Time to wait before force-killing a container, as
                integer seconds or a timedelta with up to nanosecond units.
            log_opts: Logging driver options as key-value pairs.
            sysctls: Kernel parameters as name-value pairs.
            ulimits: Ulimit options in Docker syntax such as nofile=1024:2048.
            reserve_cpu: CPUs to reserve, or None to omit the option.
            reserve_memory: Memory to reserve with Docker-supported units, or None.
            restart_delay: Delay between restart attempts, as integer seconds or
                a timedelta.
            restart_window: Window used to evaluate the restart policy, as integer
                seconds or a timedelta.
            placement_prefs: Placement preferences such as spread=node.labels.zone.
            with_registry_auth: Send registry authentication details to Swarm
                agents when True.
            container_labels: Labels applied to the service's containers.
            credential_spec: Credential spec for the managed service account, which
                Docker supports on Windows only; None omits it.
            health_start_interval: Interval between health checks during the start
                period in whole seconds, or None to omit the option.
            max_concurrent: Number of job tasks to run concurrently; Docker defaults
                to the replica count.
            memory_swap: Swap limit in bytes with Docker-supported units, or None to
                omit it. The value is forwarded unchanged: Docker CLI 29.9.3 lists
                -1 for unlimited in its help but rejects it when parsing the flag.
            memory_swappiness: Swappiness from 0 to 100, or -1 to reset the default.
            no_resolve_image: Skip querying the registry for the image digest and
                supported platforms when True.
            oom_score_adj: OOM-killer preference from -1000 to 1000, or None.
            replicas_max_per_node: Maximum tasks per node; Docker treats zero as
                unlimited.
            update_parallelism: Update batch size, zero for all tasks, or None.
            update_delay: Delay between updates in integer seconds or timedelta, or None.
            update_failure_action: pause, continue, or rollback, or None.
            update_monitor: Update monitoring duration in seconds or timedelta, or None.
            update_max_failure_ratio: Update failure ratio in [0, 1], or None.
            update_order: stop-first or start-first, or None.
            rollback_parallelism: Rollback batch size, zero for all tasks, or None.
            rollback_delay: Delay between rollbacks in seconds or timedelta, or None.
            rollback_failure_action: pause or continue, or None.
            rollback_monitor: Rollback monitoring duration in seconds or timedelta, or None.
            rollback_max_failure_ratio: Rollback failure ratio in [0, 1], or None.
            rollback_order: stop-first or start-first, or None.
            timeout: Positive finite seconds from the create CLI's own launch,
                replacing the client default; None inherits it. This is a local
                subprocess deadline, not a Docker option.

        Returns:
            A handle to the created service without an initial inspect call.

        Raises:
            TypeError: If an option or policy value has the wrong shape or type.
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds, or an option or policy value is out of
                range or unsupported.
            DockerTimeout: If the create CLI exceeds its deadline. It is killed and
                reaped, without undoing daemon-side service creation.
        """
        validate_timeout(timeout)
        policy_args = update_options(
            "update",
            update_parallelism,
            update_delay,
            update_failure_action,
            update_monitor,
            update_max_failure_ratio,
            update_order,
        ) + update_options(
            "rollback",
            rollback_parallelism,
            rollback_delay,
            rollback_failure_action,
            rollback_monitor,
            rollback_max_failure_ratio,
            rollback_order,
        )
        options = encode_options(
            "create",
            {
                "cap_add": cap_add,
                "cap_drop": cap_drop,
                "config": config,
                "constraint": constraints,
                "container_label": container_labels,
                "credential_spec": credential_spec,
                "detach": detach,
                "dns": dns,
                "dns_option": dns_options,
                "dns_search": dns_search,
                "endpoint_mode": endpoint_mode,
                "entrypoint": entrypoint,
                "env": envs,
                "env_file": env_files,
                "generic_resource": generic_resources,
                "group": groups,
                "no_healthcheck": not healthcheck,
                "health_cmd": health_cmd,
                "health_interval": health_interval,
                "health_retries": health_retries,
                "health_start_interval": health_start_interval,
                "health_start_period": health_start_period,
                "health_timeout": health_timeout,
                "host": hosts,
                "hostname": hostname,
                "init": init,
                "isolation": isolation,
                "label": labels,
                "limit_cpu": limit_cpu,
                "limit_memory": limit_memory,
                "limit_pids": limit_pids,
                "log_driver": log_driver,
                "log_opt": log_opts,
                "max_concurrent": max_concurrent,
                "memory_swap": memory_swap,
                "memory_swappiness": memory_swappiness,
                "mode": mode,
                "name": name,
                "no_resolve_image": no_resolve_image,
                "oom_score_adj": oom_score_adj,
                "placement_pref": placement_prefs,
                "publish": publish,
                "read_only": read_only,
                "replicas": replicas,
                "replicas_max_per_node": replicas_max_per_node,
                "reserve_cpu": reserve_cpu,
                "reserve_memory": reserve_memory,
                "restart_condition": restart_condition,
                "restart_delay": restart_delay,
                "restart_max_attempts": restart_max_attempts,
                "restart_window": restart_window,
                "mount": mounts,
                "network": network,
                "secret": secrets,
                "stop_grace_period": stop_grace_period,
                "stop_signal": stop_signal,
                "sysctl": sysctls,
                "tty": tty,
                "ulimit": ulimits,
                "user": user,
                "with_registry_auth": with_registry_auth,
                "workdir": workdir,
            },
            self.client_config,
        )
        full_cmd = Command([*self.docker_cmd, "service", "create", "--quiet"])
        full_cmd.extend(options.arguments)
        full_cmd.extend(policy_args)

        full_cmd.append(image)
        if command is not None:
            for string in to_list(command):
                full_cmd.append(string)

        service_id = run(full_cmd, env=options.environment, timeout=timeout)
        return Service(self.client_config, service_id, is_immutable_id=True)

    @overload
    def inspect(self, x: str, *, pretty: Literal[False] = False) -> Service:
        pass

    @overload
    def inspect(
        self, x: builtins.list[str], *, pretty: Literal[False] = False
    ) -> builtins.list[Service]: ...

    @overload
    def inspect(self, x: str | builtins.list[str], *, pretty: Literal[True]) -> str: ...

    @overload
    def inspect(
        self, x: str | builtins.list[str], *, pretty: bool
    ) -> Service | builtins.list[Service] | str: ...

    def inspect(
        self, x: str | builtins.list[str], *, pretty: bool = False
    ) -> Service | builtins.list[Service] | str:
        """Inspect one or more Swarm services by name or ID.

        Args:
            x: One name or ID, or a list of references. An empty list returns
                without invoking the CLI.
            pretty: Return Docker's human-readable report as text instead of
                handles. The text is not parsed and no snapshot is cached or
                pinned. An empty list returns an empty string.

        Returns:
            One handle for a string, otherwise handles in request order with
            duplicates retained as distinct handles. All snapshots are validated
            before any returned handle is hydrated. With `pretty=True`, the CLI
            output for every reference as one string.

        Raises:
            NoSuchService: If Docker reports that a requested service is absent.
            ParsingError: If response identities cannot be associated unambiguously
                with the requests or snapshots are inconsistent.
        """
        references = to_list(x)
        if pretty:
            if not references:
                return ""
            return run(
                [*self.docker_cmd, "service", "inspect", "--pretty", *references]
            )
        if not references:
            return []
        json_str = run([*self.docker_cmd, "service", "inspect", *references])
        parser = Service(self.client_config, references[0], is_immutable_id=True)
        snapshots = parse_swarm_inspect_results(parser, json_str, references)
        resources = []
        for identity, snapshot in snapshots:
            resource = Service(self.client_config, identity, is_immutable_id=True)
            resource._set_inspect_result(snapshot)
            resources.append(resource)
        match x:
            case str():
                return resources[0]
            case builtins.list():
                return resources
            case unreachable:
                assert_never(unreachable)

    def exists(self, x: str) -> bool:
        """Return whether a service can be inspected.

        Args:
            x: Service name or ID to inspect.

        Returns:
            False when Docker reports NoSuchService; True on successful inspection.
            Other inspection failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchService:
            return False
        else:
            return True

    @overload
    def logs(
        self,
        service: ValidService,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        task_ids: bool = True,
        resolve: bool = True,
        truncate: bool = True,
        stream: Literal[False] = False,
        *,
        timeout: float | None = None,
    ) -> str: ...

    @overload
    def logs(
        self,
        service: ValidService,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        task_ids: bool = True,
        resolve: bool = True,
        truncate: bool = True,
        *,
        stream: Literal[True],
        timeout: float | None = None,
    ) -> ProcessStream: ...

    @overload
    def logs(
        self,
        service: ValidService,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        task_ids: bool = True,
        resolve: bool = True,
        truncate: bool = True,
        stream: bool = False,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream: ...

    def logs(
        self,
        service: ValidService,
        details: bool = False,
        since: datetime | timedelta | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        follow: bool = False,
        raw: bool = False,
        task_ids: bool = True,
        resolve: bool = True,
        truncate: bool = True,
        stream: bool = False,
        *,
        timeout: float | None = None,
    ) -> str | ProcessStream:
        """Return collected service logs or an owned byte stream.

        Service inspection runs first, outside the logs deadline. The logs CLI
        starts before this call returns, including in streaming mode. Collection
        blocks until the CLI exits and its pipes drain; follow=True can wait indefinitely
        even when stream=False. Closing a stream only cancels the local CLI,
        not the service. Exhaust the stream, use it as a context manager, or call
        close() when stopping early; a bare loop break does not release it.

        Args:
            service: Service name, ID, or handle to read logs from.
            details: Include additional log details.
            since: Lower time bound, or None to omit it.
            tail: Number of trailing lines; None leaves Docker's default of all lines.
            timestamps: Include timestamps with log records.
            follow: Keep following new log records instead of taking a snapshot.
            raw: Omit Docker's log formatting.
            task_ids: Include task IDs in formatted output.
            resolve: Resolve IDs to names in formatted output.
            truncate: Allow Docker to truncate IDs in formatted output.
            stream: Return a closeable stream when True; otherwise collect decoded
                stdout and stderr records into one string in arrival order.
            timeout: Positive finite seconds for the local logs CLI, or None to
                inherit the client default. Timing starts at launch and continues while iteration
                pauses. Expiry kills and reaps the local CLI, preserving captured
                output; natural post-exit pipe draining is unchanged.

        Returns:
            Collected text when stream=False; otherwise an owned stream of
            (source, content) records, with source equal to stdout or stderr and
            content containing bytes. It inherits the client's stream buffer budget.

        Raises:
            NoSuchService: If the service inspection reports that it is absent.
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the logs CLI exceeds its deadline, delivered during
                collection or stream consumption/clean context exit.
            BufferError: If a record or retained stderr history exceeds the
                configured stream buffer budget.
        """
        validate_timeout(timeout)
        # first we verify that the service exists and raise an exception if not.
        self.inspect(str(service))

        full_cmd = logs_command(
            self.docker_cmd,
            service,
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
        else:
            with iterator:
                return "".join(x[1].decode() for x in iterator)

    def list(
        self, filters: Iterable[ServiceListFilter] = (), *, hydrate: bool = False
    ) -> builtins.list[Service]:
        """List services in the selected Swarm.

        Args:
            filters: Docker filter pairs; the default selects all services.
            hydrate: If `True`, resolve the truncated listed IDs and inspect the
                full IDs in bounded batches, so a listing costs one launch, one
                to resolve the IDs and one per chunk, and reading properties
                costs none. Snapshots stay pinned until `reload()` or a
                mutating call on the handle, and services removed before the
                resolution or the inspect are omitted. If `False`, each handle
                inspects lazily on first property access.

        Returns:
            Service handles with full IDs in CLI output order. Nonempty results
            require a second CLI call to resolve the truncated IDs returned by
            service list.

        Raises:
            ParsingError: A resolution or hydration response is malformed or does
                not match the listed IDs.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "service", "list", "--quiet"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        ids_truncated = run(full_cmd).splitlines()

        # the ids are truncated because there is no single docker command that allows us to get them
        # untruncated. We must run an inspect command to get all untruncated ids.

        if ids_truncated == []:
            return []

        if hydrate:
            full_ids = resolve_identities(
                ids_truncated,
                lambda ids: run(
                    [
                        *self.docker_cmd,
                        "service",
                        "inspect",
                        *ids,
                        "--format",
                        "{{.ID}}",
                    ]
                ),
                NoSuchService,
            )
            return hydrate_handles(
                [
                    Service(self.client_config, id_, is_immutable_id=True)
                    for id_ in full_ids
                ],
                lambda ids: run([*self.docker_cmd, "service", "inspect", *ids]),
                NoSuchService,
            )

        full_cmd = [
            *self.docker_cmd,
            "service",
            "inspect",
            *ids_truncated,
            "--format",
            "{{.ID}}",
        ]
        ids_not_truncated = run(full_cmd).splitlines()

        return [
            Service(self.client_config, x, is_immutable_id=True)
            for x in ids_not_truncated
        ]

    def ps(
        self,
        x: ValidService | Iterable[ValidService],
        *,
        filters: Iterable[ServicePsFilter] | None = None,
    ) -> builtins.list[pywhales.components.task.cli_wrapper.Task]:
        """List Swarm tasks associated with one or more services.

        Args:
            x: Service names, IDs, or handles to list tasks for. An empty
                selection returns [] without invoking the CLI.
            filters: Docker filter pairs such as `("desired-state", "running")`,
                forwarded in order with duplicates kept; None applies no filter.
                A mapping is rejected.

        Returns:
            Task handles in CLI output order, including any task history Docker lists.

        Raises:
            TypeError: If filters is a mapping.
            NoSuchService: If Docker reports that a requested service is absent.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        services = to_list(x)
        if not services:
            return []
        full_cmd = Command([*self.docker_cmd, "service", "ps", "--quiet", "--no-trunc"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters or ())
        )
        full_cmd.extend(services)
        ids = run(full_cmd).splitlines()
        return [
            pywhales.components.task.cli_wrapper.Task(
                self.client_config, id_, is_immutable_id=True
            )
            for id_ in ids
        ]

    def remove(self, services: ValidService | Iterable[ValidService]) -> None:
        """Remove one or more Swarm services.

        Args:
            services: Service names, IDs, or handles. An empty selection is a no-op.

        Raises:
            NoSuchService: If Docker reports that a requested service is absent.
        """
        selected = to_list(services)
        if not selected:
            return
        full_cmd = Command([*self.docker_cmd, "service", "remove"])

        for service in selected:
            full_cmd.append(service)

        try:
            run(full_cmd)
        finally:
            release_pins(selected)

    def rollback(
        self,
        service: ValidService,
        *,
        detach: bool = False,
        quiet: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Roll back one service without inspecting or retrying the mutation.

        Success indicates CLI completion, not application health. An interrupted
        command can leave the remote outcome unknown; resources remain operator-owned.

        Args:
            service: Service name, ID, or immutable handle.
            detach: Return after submission rather than waiting for convergence.
            quiet: Request only the service ID from Docker.
            timeout: Positive finite seconds from the rollback CLI's own launch,
                replacing the client default; None inherits it.

        Raises:
            NoSuchService: If Docker reports that the service is absent.
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the CLI exceeds its deadline. It is killed and reaped,
                without undoing the rollback or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "service", "rollback"])
        full_cmd.add_flag("--detach", detach)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.append(service)
        try:
            run(full_cmd, capture_stdout=False, timeout=timeout)
        finally:
            release_pins([service])

    def scale(
        self,
        new_scales: dict[ValidService, int],
        detach: bool = False,
        *,
        timeout: float | None = None,
    ) -> None:
        """Scale one or more services.

        Args:
            new_scales: Mapping between services and the desired scales. For example
                use new_scales={"service1": 4, "service2": 8}. An empty mapping
                is a no-op.
            detach: Return after submission instead of waiting for convergence.
            timeout: Positive finite seconds from the scale CLI's own launch,
                replacing the client default; None inherits it. The service
                inspection preflight is outside this budget.

        Raises:
            NoSuchService: If inspection reports that a requested service is absent.
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds.
            DockerTimeout: If the scale CLI exceeds its deadline. It is killed and
                reaped, without undoing the scale or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        if not new_scales:
            return
        try:
            # verify that the services exists
            self.inspect([str(service) for service in new_scales])

            full_cmd = Command([*self.docker_cmd, "service", "scale"])
            full_cmd.add_flag("--detach", detach)
            for service, new_scale in new_scales.items():
                full_cmd.append(f"{service!s}={new_scale}")
            run(full_cmd, capture_stderr=False, capture_stdout=False, timeout=timeout)
        finally:
            release_pins(new_scales)

    def update(
        self,
        service: ValidService,
        detach: bool = False,
        force: bool = False,
        image: str | None = None,
        with_registry_authentication: bool = False,
        quiet: bool = False,
        replicas: int | None = None,
        *,
        update_parallelism: int | None = None,
        update_delay: int | timedelta | None = None,
        update_failure_action: UpdateFailureActionInput | None = None,
        update_monitor: int | timedelta | None = None,
        update_max_failure_ratio: float | None = None,
        update_order: UpdateOrderInput | None = None,
        rollback_parallelism: int | None = None,
        rollback_delay: int | timedelta | None = None,
        rollback_failure_action: RollbackFailureActionInput | None = None,
        rollback_monitor: int | timedelta | None = None,
        rollback_max_failure_ratio: float | None = None,
        rollback_order: UpdateOrderInput | None = None,
        args: Sequence[str] | None = None,
        cap_add: Sequence[str] = (),
        cap_drop: Sequence[str] = (),
        config_add: Sequence[ServiceLongOption] = (),
        config_rm: Sequence[str] = (),
        constraint_add: Sequence[str] = (),
        constraint_rm: Sequence[str] = (),
        container_label_add: Mapping[str, str] = MappingProxyType({}),
        container_label_rm: Sequence[str] = (),
        credential_spec: str | None = None,
        dns_add: Sequence[str] = (),
        dns_rm: Sequence[str] = (),
        dns_option_add: Sequence[str] = (),
        dns_option_rm: Sequence[str] = (),
        dns_search_add: Sequence[str] = (),
        dns_search_rm: Sequence[str] = (),
        endpoint_mode: str | None = None,
        entrypoint: str | None = None,
        env_add: Mapping[str, str] = MappingProxyType({}),
        env_rm: Sequence[str] = (),
        generic_resource_add: Sequence[str] = (),
        generic_resource_rm: Sequence[str] = (),
        group_add: Sequence[str] = (),
        group_rm: Sequence[str] = (),
        health_cmd: str | None = None,
        health_interval: int | timedelta | None = None,
        health_retries: int | None = None,
        health_start_interval: int | timedelta | None = None,
        health_start_period: int | timedelta | None = None,
        health_timeout: int | timedelta | None = None,
        host_add: Mapping[str, str] = MappingProxyType({}),
        host_rm: Mapping[str, str] = MappingProxyType({}),
        hostname: str | None = None,
        init: bool | None = None,
        isolation: str | None = None,
        label_add: Mapping[str, str] = MappingProxyType({}),
        label_rm: Sequence[str] = (),
        limit_cpu: float | None = None,
        limit_memory: str | None = None,
        limit_pids: int | None = None,
        log_driver: str | None = None,
        log_opts: Mapping[str, str] = MappingProxyType({}),
        max_concurrent: int | None = None,
        memory_swap: int | str | None = None,
        memory_swappiness: int | None = None,
        mount_add: Sequence[ServiceLongOption] = (),
        mount_rm: Sequence[str] = (),
        network_add: Sequence[str] = (),
        network_rm: Sequence[str] = (),
        no_healthcheck: bool = False,
        no_resolve_image: bool = False,
        oom_score_adj: int | None = None,
        placement_pref_add: Sequence[str] = (),
        placement_pref_rm: Sequence[str] = (),
        publish_add: Iterable[ServicePort] = (),
        publish_rm: Iterable[ServicePort] = (),
        read_only: bool | None = None,
        replicas_max_per_node: int | None = None,
        reserve_cpu: float | None = None,
        reserve_memory: str | None = None,
        restart_condition: RestartConditionInput | None = None,
        restart_delay: int | timedelta | None = None,
        restart_max_attempts: int | None = None,
        restart_window: int | timedelta | None = None,
        rollback: bool = False,
        secret_add: Sequence[ServiceLongOption] = (),
        secret_rm: Sequence[str] = (),
        stop_grace_period: int | timedelta | None = None,
        stop_signal: str | None = None,
        sysctl_add: Mapping[str, str] = MappingProxyType({}),
        sysctl_rm: Sequence[str] = (),
        tty: bool | None = None,
        ulimit_add: Sequence[str] = (),
        ulimit_rm: Sequence[str] = (),
        user: str | None = None,
        workdir: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Update a Swarm service.

        Success means the selected CLI invocation completed, not application
        health. Interruption can leave the remote outcome unknown. This method
        does not probe, retry, or assume ownership of the service. Options after
        `replicas` are keyword-only. Docker applies `*_add` and `*_rm` changes
        to the service's current specification, and options you leave out keep
        their current settings. Every value is validated before the CLI
        executable is resolved.

        Args:
            service: The service to update
            detach: Exit immediately instead of waiting for the service to converge
            force: Force update even if no changes require it
            image: Service image reference, or None to leave it unchanged.
            with_registry_authentication: Send registry authentication details
                to Swarm agents.
            quiet: Request only the service ID from Docker.
            replicas: Desired replica count, or None to leave it unchanged.
            update_parallelism: Update batch size, zero for all tasks, or None.
            update_delay: Delay between updates in integer seconds or timedelta, or None.
            update_failure_action: pause, continue, or rollback, or None.
            update_monitor: Update monitoring duration in seconds or timedelta, or None.
            update_max_failure_ratio: Update failure ratio in [0, 1], or None.
            update_order: stop-first or start-first, or None.
            rollback_parallelism: Rollback batch size, zero for all tasks, or None.
            rollback_delay: Delay between rollbacks in seconds or timedelta, or None.
            rollback_failure_action: pause or continue, or None.
            rollback_monitor: Rollback monitoring duration in seconds or timedelta, or None.
            rollback_max_failure_ratio: Rollback failure ratio in [0, 1], or None.
            rollback_order: stop-first or start-first, or None.
            args: Replacement command arguments, or None to leave them unchanged. They are
                shell-quoted into one `--args` value, so an empty sequence clears them.
            cap_add: Linux capabilities to add.
            cap_drop: Linux capabilities to drop.
            config_add: Configs to add or update, each a mapping of long-form keys such as
                source, target, uid, gid and mode.
            config_rm: Names of configs to remove.
            constraint_add: Placement constraints to add or update.
            constraint_rm: Placement constraints to remove.
            container_label_add: Container labels to add or update.
            container_label_rm: Keys of container labels to remove.
            credential_spec: Credential spec for the managed service account, which Docker supports on
                Windows only; None leaves it unchanged.
            dns_add: Custom DNS servers to add or update.
            dns_rm: Custom DNS servers to remove.
            dns_option_add: DNS options to add or update.
            dns_option_rm: DNS options to remove.
            dns_search_add: DNS search domains to add or update.
            dns_search_rm: DNS search domains to remove.
            endpoint_mode: Endpoint mode, vip or dnsrr, or None to leave it unchanged.
            entrypoint: Entrypoint override, or None to leave it unchanged.
            env_add: Environment variables to add or update. Eligible values reach the CLI
                through the child environment, not argv, as for `create`.
            env_rm: Names of environment variables to remove.
            generic_resource_add: Generic resources to add, such as gpu=2.
            generic_resource_rm: Names of generic resources to remove.
            group_add: Supplementary groups to add.
            group_rm: Supplementary groups to remove.
            health_cmd: Health-check command, or None to leave it unchanged.
            health_interval: Interval between health checks in whole seconds, or None to leave it unchanged.
            health_retries: Failures before marking a container unhealthy, or None to leave it unchanged.
            health_start_interval: Interval between health checks during the start period in whole seconds,
                or None to leave it unchanged.
            health_start_period: Health-check startup grace period in whole seconds, or None to leave it
                unchanged.
            health_timeout: Health-check timeout in whole seconds, or None to leave it unchanged.
            host_add: Host-to-IP mappings to add.
            host_rm: Host-to-IP mappings to remove; each pair must match an existing entry.
            hostname: Container hostname, or None to leave it unchanged.
            init: Run an init process inside service containers; False turns it off and None
                leaves it unchanged.
            isolation: Container isolation mode, or None to leave it unchanged.
            label_add: Service labels to add or update.
            label_rm: Keys of service labels to remove.
            limit_cpu: CPU limit, or None to leave it unchanged.
            limit_memory: Memory limit with Docker-supported units, or None to leave it unchanged.
            limit_pids: PID limit, or None to leave it unchanged.
            log_driver: Logging driver, or None to leave it unchanged.
            log_opts: Logging driver options as key-value pairs.
            max_concurrent: Number of job tasks to run concurrently, or None to leave it unchanged.
            memory_swap: Swap limit in bytes with Docker-supported units, or None to
                leave it unchanged. The value is forwarded unchanged: Docker CLI
                29.9.3 lists -1 for unlimited in its help but rejects it when
                parsing the flag.
            memory_swappiness: Swappiness from 0 to 100, or -1 to reset the default; None leaves it
                unchanged.
            mount_add: Mounts to add or update, each a mapping of long-form keys such as type,
                source and target.
            mount_rm: Target paths of mounts to remove.
            network_add: Network attachment specifications to add.
            network_rm: Networks to remove.
            no_healthcheck: Disable any container-specified health check when True.
            no_resolve_image: Skip querying the registry for the image digest and supported platforms
                when True.
            oom_score_adj: OOM-killer preference from -1000 to 1000, or None to leave it unchanged.
            placement_pref_add: Placement preferences to add, such as spread=node.labels.zone.
            placement_pref_rm: Placement preferences to remove.
            publish_add: Ports to add or update, as port tuples or long-form mappings as in `create`.
            publish_rm: Published ports to remove, in the same forms, matched by target port,
                protocol and publish mode.
            read_only: Mount the container root filesystem read-only; False turns it off and None
                leaves it unchanged.
            replicas_max_per_node: Maximum tasks per node, where zero means unlimited; None leaves it unchanged.
            reserve_cpu: CPUs to reserve, or None to leave it unchanged.
            reserve_memory: Memory to reserve with Docker-supported units, or None to leave it unchanged.
            restart_condition: Restart policy condition, or None to leave it unchanged.
            restart_delay: Delay between restart attempts in integer seconds or a timedelta, or None.
            restart_max_attempts: Maximum restart attempts, or None to leave it unchanged.
            restart_window: Window used to evaluate the restart policy in integer seconds or a
                timedelta, or None.
            rollback: Roll back to the previous specification when True. The CLI rejects other
                options alongside it, except detach and quiet.
            secret_add: Secrets to add or update, each a mapping of long-form keys such as source,
                target, uid, gid and mode.
            secret_rm: Names of secrets to remove.
            stop_grace_period: Time to wait before force-killing a container, in integer seconds or a
                timedelta, or None.
            stop_signal: Signal that stops the container, or None.
            sysctl_add: Kernel parameters to add or update.
            sysctl_rm: Names of kernel parameters to remove.
            tty: Allocate a pseudo-TTY; False turns it off and None leaves it unchanged.
            ulimit_add: Ulimit options to add or update, such as nofile=1024:2048.
            ulimit_rm: Names of ulimit options to remove.
            user: Username or UID, optionally with a group, or None.
            workdir: Working directory inside the container, or None.
            timeout: Positive finite seconds from the update CLI's own launch,
                replacing the client default; None inherits it. This is a local
                subprocess deadline, not a Docker option.

        Raises:
            NoSuchService: If Docker reports that the service is absent.
            TypeError: If a policy or option value has an invalid type or shape.
            ValueError: If a policy or option value is out of range or
                unsupported, or if timeout is a Boolean or is neither None nor a
                positive finite number of seconds.
            DockerTimeout: If the CLI exceeds its deadline. It is killed and reaped,
                without undoing the update or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        policy_args = update_options(
            "update",
            update_parallelism,
            update_delay,
            update_failure_action,
            update_monitor,
            update_max_failure_ratio,
            update_order,
        ) + update_options(
            "rollback",
            rollback_parallelism,
            rollback_delay,
            rollback_failure_action,
            rollback_monitor,
            rollback_max_failure_ratio,
            rollback_order,
        )
        options = encode_options(
            "update",
            {
                "replicas": replicas,
                "args": args,
                "cap_add": cap_add,
                "cap_drop": cap_drop,
                "config_add": config_add,
                "config_rm": config_rm,
                "constraint_add": constraint_add,
                "constraint_rm": constraint_rm,
                "container_label_add": container_label_add,
                "container_label_rm": container_label_rm,
                "credential_spec": credential_spec,
                "dns_add": dns_add,
                "dns_rm": dns_rm,
                "dns_option_add": dns_option_add,
                "dns_option_rm": dns_option_rm,
                "dns_search_add": dns_search_add,
                "dns_search_rm": dns_search_rm,
                "endpoint_mode": endpoint_mode,
                "entrypoint": entrypoint,
                "env_add": env_add,
                "env_rm": env_rm,
                "generic_resource_add": generic_resource_add,
                "generic_resource_rm": generic_resource_rm,
                "group_add": group_add,
                "group_rm": group_rm,
                "health_cmd": health_cmd,
                "health_interval": health_interval,
                "health_retries": health_retries,
                "health_start_interval": health_start_interval,
                "health_start_period": health_start_period,
                "health_timeout": health_timeout,
                "host_add": host_add,
                "host_rm": host_rm,
                "hostname": hostname,
                "init": init,
                "isolation": isolation,
                "label_add": label_add,
                "label_rm": label_rm,
                "limit_cpu": limit_cpu,
                "limit_memory": limit_memory,
                "limit_pids": limit_pids,
                "log_driver": log_driver,
                "log_opt": log_opts,
                "max_concurrent": max_concurrent,
                "memory_swap": memory_swap,
                "memory_swappiness": memory_swappiness,
                "mount_add": mount_add,
                "mount_rm": mount_rm,
                "network_add": network_add,
                "network_rm": network_rm,
                "no_healthcheck": no_healthcheck,
                "no_resolve_image": no_resolve_image,
                "oom_score_adj": oom_score_adj,
                "placement_pref_add": placement_pref_add,
                "placement_pref_rm": placement_pref_rm,
                "publish_add": publish_add,
                "publish_rm": publish_rm,
                "read_only": read_only,
                "replicas_max_per_node": replicas_max_per_node,
                "reserve_cpu": reserve_cpu,
                "reserve_memory": reserve_memory,
                "restart_condition": restart_condition,
                "restart_delay": restart_delay,
                "restart_max_attempts": restart_max_attempts,
                "restart_window": restart_window,
                "rollback": rollback,
                "secret_add": secret_add,
                "secret_rm": secret_rm,
                "stop_grace_period": stop_grace_period,
                "stop_signal": stop_signal,
                "sysctl_add": sysctl_add,
                "sysctl_rm": sysctl_rm,
                "tty": tty,
                "ulimit_add": ulimit_add,
                "ulimit_rm": ulimit_rm,
                "user": user,
                "workdir": workdir,
            },
            self.client_config,
        )
        full_cmd = Command([*self.docker_cmd, "service", "update"])
        full_cmd.add_flag("--force", force)
        full_cmd.add_simple_arg("--image", image)
        full_cmd.add_flag("--with-registry-auth", with_registry_authentication)
        full_cmd.add_flag("--detach", detach)
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.extend(options.arguments)
        full_cmd.extend(policy_args)
        full_cmd.append(service)
        try:
            run(
                full_cmd,
                capture_stdout=False,
                env=options.environment,
                timeout=timeout,
            )
        finally:
            release_pins([service])
