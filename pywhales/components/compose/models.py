"""Typed subsets of Compose configuration, project summaries, events and listings."""

from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from pywhales.enums import (
    ComposeDependencyCondition,
    MountType,
    NetworkProtocol,
    PublishMode,
)


class ServicePlacement(BaseModel):
    constraints: list[str] | None = None


class ResourcesLimits(BaseModel):
    cpus: float | None = None
    memory: int | None = None


class ResourcesReservation(BaseModel):
    cpus: float | int | None = None
    memory: int | None = None


class ServiceResources(BaseModel):
    limits: ResourcesLimits | None = None
    reservations: ResourcesReservation | None = None


class ServiceDeployConfig(BaseModel):
    labels: dict[str, str] | None = None
    resources: ServiceResources | None = None
    placement: ServicePlacement | None = None
    replicas: int | None = None


class DependencyCondition(BaseModel):
    condition: ComposeDependencyCondition | str | None = None


class ComposeServiceBuild(BaseModel):
    context: Path | None = None
    dockerfile: Path | None = None
    args: dict[str, JsonValue] | None = None
    labels: dict[str, JsonValue] | None = None


class ComposeServicePort(BaseModel):
    mode: PublishMode | str | None = None
    protocol: NetworkProtocol | str | None = None
    published: int | None = None
    target: int | None = None


class ComposeServiceVolume(BaseModel):
    bind: dict[str, JsonValue] | None = None
    source: str | None = None
    target: str | None = None
    type: MountType | str | None = None


class ComposeServiceNetwork(BaseModel):
    aliases: list[str] | None = None
    ipv4_address: str | None = None
    ipv6_address: str | None = None
    link_local_ips: list[str] | None = None
    mac_address: str | None = None
    driver_opts: dict[str, JsonValue] | None = None
    gw_priority: int | None = None
    priority: int | None = None


class ComposeConfigService(BaseModel):
    deploy: ServiceDeployConfig | None = None
    blkio_config: JsonValue | None = None
    cpu_count: float | None = None
    cpu_percent: float | None = None
    cpu_shares: int | None = None
    cpuset: str | None = None
    build: ComposeServiceBuild | None = None
    cap_add: Annotated[list[str] | None, Field(default_factory=list)]
    cap_drop: Annotated[list[str] | None, Field(default_factory=list)]
    cgroup_parent: str | None = None
    command: list[str] | str | None = None
    configs: JsonValue = None
    container_name: str | None = None
    depends_on: Annotated[dict[str, DependencyCondition], Field(default_factory=dict)]
    device_cgroup_rules: Annotated[list[str], Field(default_factory=list)]
    devices: JsonValue = None
    environment: dict[str, str | int | None] | list[str] | None = None
    expose: Annotated[list[int] | list[str], Field(default_factory=list)]
    entrypoint: list[str] | str | None = None
    hostname: str | None = None
    image: str | None = None
    labels: Annotated[dict[str, str] | None, Field(default_factory=dict)]
    ports: list[ComposeServicePort] | None = None
    volumes: list[ComposeServiceVolume] | None = None
    networks: dict[str, ComposeServiceNetwork | None] | None = None


class ComposeConfigNetwork(BaseModel):
    driver: str | None = None
    name: str | None = None
    external: bool | None = False
    driver_opts: dict[str, JsonValue] | None = None
    attachable: bool | None = None
    enable_ipv6: bool | None = None
    ipam: JsonValue = None
    internal: bool | None = None
    labels: Annotated[dict[str, str], Field(default_factory=dict)]


class ComposeConfigVolume(BaseModel):
    driver: str | None = None
    driver_opts: dict[str, JsonValue] | None = None
    external: bool | None = None
    labels: Annotated[dict[str, str] | None, Field(default_factory=dict)]
    name: str | None = None


class ComposeConfig(BaseModel):
    """Supported resolved project fields, without live service ownership."""

    services: dict[str, ComposeConfigService] | None = None
    networks: Annotated[
        dict[str, ComposeConfigNetwork] | None, Field(default_factory=dict)
    ]
    volumes: Annotated[
        dict[str, ComposeConfigVolume] | None, Field(default_factory=dict)
    ]
    configs: JsonValue = None
    secrets: JsonValue = None


class ComposeProject(BaseModel):
    name: str | None
    created: int | None = 0
    running: int | None = 0
    restarting: int | None = 0
    exited: int | None = 0
    paused: int | None = 0
    dead: int | None = 0
    config_files: list[Path] | None = None


class ComposeEvent(BaseModel):
    action: Annotated[str | None, Field(alias="action")] = None
    attributes: Annotated[dict[str, str] | None, Field(alias="attributes")] = None
    id: Annotated[str | None, Field(alias="id")] = None
    service: Annotated[str | None, Field(alias="service")] = None
    time: Annotated[datetime | None, Field(alias="time")] = None
    type: Annotated[str | None, Field(alias="type")] = None


class _ComposeWireModel(BaseModel):
    """Base for listings that map the CLI's capitalized JSON names explicitly."""

    model_config = ConfigDict(populate_by_name=True)


class ComposeImage(_ComposeWireModel):
    """One image used by a created project container, from `images --format json`."""

    id: Annotated[str | None, Field(alias="ID")] = None
    container_name: Annotated[str | None, Field(alias="ContainerName")] = None
    repository: Annotated[str | None, Field(alias="Repository")] = None
    tag: Annotated[str | None, Field(alias="Tag")] = None
    platform: Annotated[str | None, Field(alias="Platform")] = None
    size: Annotated[int | None, Field(alias="Size")] = None
    created: Annotated[datetime | None, Field(alias="Created")] = None
    last_tag_time: Annotated[datetime | None, Field(alias="LastTagTime")] = None


class ComposeVolume(_ComposeWireModel):
    """One project volume, from `volumes --format json`.

    Every value is the CLI's display text, including `labels` (comma-joined
    `key=value` pairs) and sizes such as `N/A`.
    """

    availability: Annotated[str | None, Field(alias="Availability")] = None
    driver: Annotated[str | None, Field(alias="Driver")] = None
    group: Annotated[str | None, Field(alias="Group")] = None
    labels: Annotated[str | None, Field(alias="Labels")] = None
    links: Annotated[str | None, Field(alias="Links")] = None
    mountpoint: Annotated[str | None, Field(alias="Mountpoint")] = None
    name: Annotated[str | None, Field(alias="Name")] = None
    scope: Annotated[str | None, Field(alias="Scope")] = None
    size: Annotated[str | None, Field(alias="Size")] = None
    status: Annotated[str | None, Field(alias="Status")] = None


class ComposeStats(_ComposeWireModel):
    """One resource-usage sample, from `stats --no-stream --format json`.

    Usage values keep the CLI's display text, such as `0.03%` or
    `2.2MiB / 7.7GiB`. Per-container rows set `container`, `id` and `name`;
    `all_projects` rows set `project` and `name`; `aggregate` rows set `project`
    and `containers`. Process counts are strings for per-container rows and
    integers elsewhere.
    """

    project: Annotated[str | None, Field(alias="Project")] = None
    container: Annotated[str | None, Field(alias="Container")] = None
    id: Annotated[str | None, Field(alias="ID")] = None
    name: Annotated[str | None, Field(alias="Name")] = None
    containers: Annotated[int | None, Field(alias="Containers")] = None
    cpu_perc: Annotated[str | None, Field(alias="CPUPerc")] = None
    mem_usage: Annotated[str | None, Field(alias="MemUsage")] = None
    mem_perc: Annotated[str | None, Field(alias="MemPerc")] = None
    net_io: Annotated[str | None, Field(alias="NetIO")] = None
    block_io: Annotated[str | None, Field(alias="BlockIO")] = None
    pids: Annotated[str | int | None, Field(alias="PIDs")] = None


class ComposeVariable(_ComposeWireModel):
    """One interpolation variable found in the project, from `config --variables`."""

    name: Annotated[str | None, Field(alias="Name")] = None
    default_value: Annotated[str | None, Field(alias="DefaultValue")] = None
    presence_value: Annotated[str | None, Field(alias="PresenceValue")] = None
    required: Annotated[bool | None, Field(alias="Required")] = None


class ComposeTransformation(_ComposeWireModel):
    """One Compose Bridge transformation image, as the Docker image summary."""

    id: Annotated[str | None, Field(alias="Id")] = None
    parent_id: Annotated[str | None, Field(alias="ParentId")] = None
    repo_tags: Annotated[list[str] | None, Field(alias="RepoTags")] = None
    repo_digests: Annotated[list[str] | None, Field(alias="RepoDigests")] = None
    labels: Annotated[dict[str, str] | None, Field(alias="Labels")] = None
    created: Annotated[datetime | None, Field(alias="Created")] = None
    size: Annotated[int | None, Field(alias="Size")] = None
    shared_size: Annotated[int | None, Field(alias="SharedSize")] = None
    containers: Annotated[int | None, Field(alias="Containers")] = None
    descriptor: Annotated[dict[str, JsonValue] | None, Field(alias="Descriptor")] = None
