"""Typed Docker system information, disk summaries, and event records."""

from datetime import datetime
from pathlib import Path
from typing import Annotated

import pydantic

import pywhales.components.node.models
from pywhales.enums import Isolation, SwarmState
from pywhales.utils import DockerCamelModel


class DockerEventActor(DockerCamelModel):
    id: Annotated[str | None, pydantic.Field(alias="ID")] = None
    attributes: Annotated[
        dict[str, pydantic.JsonValue] | None, pydantic.Field(alias="Attributes")
    ] = None


class DockerEvent(DockerCamelModel):
    type: Annotated[str | None, pydantic.Field(alias="Type")] = None
    action: Annotated[str | None, pydantic.Field(alias="Action")] = None
    actor: Annotated[DockerEventActor | None, pydantic.Field(alias="Actor")] = None
    time: Annotated[datetime | None, pydantic.Field(alias="time")] = None
    time_nano: Annotated[int | None, pydantic.Field(alias="timeNano")] = None


class DockerItemsSummary(DockerCamelModel):
    active: int | None = None
    reclaimable: pydantic.ByteSize | None = None
    reclaimable_percent: float | None = None
    size: pydantic.ByteSize | None = None
    total_count: int | None = None


class Plugins(DockerCamelModel):
    volume: list[str] | None = None
    network: list[str] | None = None
    authorization: pydantic.JsonValue = None
    log: list[str] | None = None


class Runtime(DockerCamelModel):
    path: str | None = None
    runtime_args: list[str] | None = None


class DeviceInfo(DockerCamelModel):
    """A device the daemon's device driver discovered, such as a CDI device.

    Attributes:
        source: Discovering driver; Docker 28 and later report ``"cdi"``.
        id: Device name in the driver's syntax, for example
            ``"nvidia.com/gpu=0"`` for CDI.
    """

    source: str | None = None
    id: Annotated[str | None, pydantic.Field(alias="ID")] = None


class Commit(DockerCamelModel):
    id: Annotated[str | None, pydantic.Field(alias="ID")] = None
    expected: str | None = None


class RemoteManager(DockerCamelModel):
    node_id: Annotated[str | None, pydantic.Field(alias="NodeID")] = None
    addr: str | None = None


class Orchestration(DockerCamelModel):
    task_history_retention_limit: int | None = None


class Raft(DockerCamelModel):
    snapshot_interval: int | None = None
    keep_old_snapshots: int | None = None
    log_entries_for_slow_followers: int | None = None
    election_tick: int | None = None
    heartbeat_tick: int | None = None


class SwarmDispatcher(DockerCamelModel):
    heartbeat_period: int | None = None


class SwarmCAConfig(DockerCamelModel):
    node_cert_expiry: int | None = None
    external_ca: Annotated[
        list[pydantic.JsonValue] | None, pydantic.Field(alias="ExternalCA")
    ] = None
    signing_ca_cert: Annotated[str | None, pydantic.Field(alias="SigningCACert")] = None
    signing_ca_key: Annotated[str | None, pydantic.Field(alias="SigningCAKey")] = None
    force_rotate: int | None = None


class SwarmEncryptionConfig(DockerCamelModel):
    auto_lock_managers: bool | None = None


class Driver(DockerCamelModel):
    name: str | None = None
    options: dict[str, pydantic.JsonValue] | None = None


class SwarmTasksDefault(DockerCamelModel):
    log_driver: Driver | None = None


class SwarmSpec(DockerCamelModel):
    name: str | None = None
    labels: dict[str, str] | None = None
    orchestration: Orchestration | None = None
    raft: Raft | None = None
    dispatcher: SwarmDispatcher | None = None
    ca_config: Annotated[SwarmCAConfig | None, pydantic.Field(alias="CAConfig")] = None
    encryption_config: SwarmEncryptionConfig | None = None
    task_defaults: SwarmTasksDefault | None = None


class ClusterInfo(DockerCamelModel):
    id: Annotated[str | None, pydantic.Field(alias="ID")]
    version: pywhales.components.node.models.NodeVersion | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    spec: SwarmSpec | None = None
    tls_info: pywhales.components.node.models.NodeTLSInfo | None = None
    root_rotation_in_progress: bool | None = None
    data_path_port: int | None = None
    default_addr_pool: list[str] | None = None
    subnet_size: int | None = None


class SwarmInfo(DockerCamelModel):
    node_id: Annotated[str | None, pydantic.Field(alias="NodeID")] = None
    node_addr: str | None = None
    local_node_state: SwarmState | str | None = None
    control_available: bool | None = None
    error: str | None = None
    remote_managers: list[RemoteManager] | None = None
    nodes: int | None = None
    managers: int | None = None
    cluster: ClusterInfo | None = None


class ClientPlugin(DockerCamelModel):
    schema_version: str | None = None
    vendor: str | None = None
    version: str | None = None
    short_description: str | None = None
    name: str | None = None
    path: Path | None = None
    shadowed_paths: list[Path] | None = None


class ClientInfo(DockerCamelModel):
    debug: bool | None = None
    plugins: list[ClientPlugin] | None = None
    warnings: list[str] | None = None


class SystemInfo(DockerCamelModel):
    id: Annotated[str | None, pydantic.Field(alias="ID")] = None
    containers: int | None = None
    containers_running: int | None = None
    containers_paused: int | None = None
    containers_stopped: int | None = None
    images: int | None = None
    driver: str | None = None
    driver_status: list[list[str]] | None = None
    docker_root_dir: Path | None = None
    system_status: list[str] | None = None
    plugins: Plugins | None = None
    memory_limit: bool | None = None
    swap_limit: bool | None = None
    kernel_memory: bool | None = None
    cpu_cfs_period: bool | None = None
    cpu_cfs_quota: bool | None = None
    cpu_shares: Annotated[bool | None, pydantic.Field(alias="CPUShares")] = None
    cpu_set: Annotated[bool | None, pydantic.Field(alias="CPUSet")] = None
    pids_limit: bool | None = None
    oom_kill_disable: bool | None = None
    ipv4_forwarding: Annotated[bool | None, pydantic.Field(alias="IPv4Forwarding")] = (
        None
    )
    bridge_nf_iptables: bool | None = None
    bridge_nf_ip6tables: Annotated[
        bool | None, pydantic.Field(alias="BridgeNfIp6tables")
    ] = None
    debug: bool | None = None
    nfd: Annotated[int | None, pydantic.Field(alias="NFd")] = None
    n_goroutines: int | None = None
    system_time: str | None = None
    logging_driver: str | None = None
    cgroup_driver: str | None = None
    n_events_listener: int | None = None
    kernel_version: str | None = None
    operating_system: str | None = None
    os_type: Annotated[str | None, pydantic.Field(alias="OSType")] = None
    architecture: str | None = None
    n_cpu: Annotated[int | None, pydantic.Field(alias="NCPU")] = None
    mem_total: int | None = None
    index_server_address: str | None = None
    registry_config: dict[str, pydantic.JsonValue] | None = None
    generic_resources: (
        list[pywhales.components.node.models.NodeGenericResource] | None
    ) = None

    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    name: str | None = None
    labels: list[str] | None = None
    experimental_build: bool | None = None
    server_version: str | None = None
    cluster_store: str | None = None
    runtimes: dict[str, Runtime] | None = None
    default_runtime: str | None = None
    cdi_spec_dirs: Annotated[list[str] | None, pydantic.Field(alias="CDISpecDirs")] = (
        None
    )
    discovered_devices: list[DeviceInfo] | None = None
    swarm: SwarmInfo | None = None
    live_restore_enabled: bool | None = None
    isolation: Isolation | str | None = None
    init_binary: str | None = None
    containerd_commit: Commit | None = None
    runc_commit: Commit | None = None
    init_commit: Commit | None = None
    security_options: list[str] | None = None
    product_license: str | None = None
    warnings: list[str] | None = None
    client_info: ClientInfo | None = None
