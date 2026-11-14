"""Typed builder-list and disk-usage records, separate from live Buildx handles."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import Field

from pywhales.utils import DockerCamelModel


class BuilderNode(DockerCamelModel):
    name: str | None = None
    endpoint: str | None = None
    flags: list[str] | None = None
    status: str | None = None
    version: str | None = None
    ids: Annotated[list[str] | None, Field(alias="IDs")] = None
    platforms: list[str] | None = None
    labels: dict[str, str] | None = None


class BuilderInspectResult(DockerCamelModel):
    name: str | None = None
    driver: str | None = None
    last_activity: dt.datetime | None = None
    dynamic: bool | None = None
    nodes: list[BuilderNode] | None = None


class BuildxDiskUsage(DockerCamelModel):
    """One build-cache record from `buildx du --format json`.

    Every field is optional so a newer Buildx can omit or add fields. Sizes and
    times keep the CLI's display text: `size` is a unit string such as
    `4.128kB`, `created_at` is a timestamp string without ISO form, and
    `last_used_at` is a relative phrase such as `34 hours ago`.
    """

    id: Annotated[str | None, Field(alias="ID")] = None
    parents: list[str] | None = None
    created_at: str | None = None
    mutable: bool | None = None
    reclaimable: bool | None = None
    shared: bool | None = None
    size: str | None = None
    description: str | None = None
    usage_count: int | None = None
    last_used_at: str | None = None
    type: str | None = None
