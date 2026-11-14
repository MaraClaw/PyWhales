"""Typed Buildx build-record listings and inspection details.

`BuildRecord` mirrors one row of `docker buildx history ls --format json`, which
uses snake_case keys. `BuildRecordDetail` mirrors `docker buildx history inspect
--format json`, which uses Go field names such as `StartedAt`.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

import pydantic

from pywhales.utils import DockerCamelModel


class BuildRecord(pydantic.BaseModel):
    """One build record from a history listing.

    Attributes:
        ref: Builder-qualified reference as listed, such as
            `default/default/<id>`. Other history commands take the bare `id`.
        name: Build name chosen by Buildx, usually from the context directory.
        status: Status text as listed, such as `Completed` or `Error`.
    """

    ref: str
    name: str | None = None
    status: str | None = None
    created_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    total_steps: int | None = None
    completed_steps: int | None = None
    cached_steps: int | None = None

    @property
    def id(self) -> str:
        """Return the bare record ID that the other history commands accept."""
        return self.ref.rsplit("/", 1)[-1]


class BuildRecordKeyValue(DockerCamelModel):
    """Build argument, label, or named-context entry of a build record."""

    name: str | None = None
    value: str | None = None


class BuildRecordConfig(DockerCamelModel):
    """Build configuration recorded for a build.

    Options Buildx reports beyond the typed fields stay available in
    `model_extra` as decoded JSON values.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    image_resolve_mode: str | None = None
    no_cache: bool | None = None


class BuildRecordMaterial(DockerCamelModel):
    """Source material that a build consumed, identified by package URL."""

    uri: Annotated[str | None, pydantic.Field(alias="URI")] = None
    digests: list[str] | None = None


class BuildRecordAttachment(DockerCamelModel):
    """Image, provenance, or other artifact attached to a build record."""

    digest: str | None = None
    type: str | None = None
    platform: str | None = None


class BuildRecordError(DockerCamelModel):
    """Failure details of a build record.

    `sources` and `stack` are base64 text exactly as Buildx encodes them. Other
    reported fields stay available in `model_extra` as decoded JSON values.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    code: int | None = None
    message: str | None = None
    name: str | None = None
    sources: str | None = None
    stack: str | None = None


class BuildRecordDetail(DockerCamelModel):
    """Inspection snapshot of one build record.

    Fields Buildx reports beyond the typed ones stay available in `model_extra`
    as decoded JSON values.

    Attributes:
        ref: Bare record ID.
        duration: Build duration in nanoseconds, as Buildx reports it.
        status: Lowercase status text as inspected, such as `completed` or
            `failed`.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    ref: str
    name: str | None = None
    context: str | None = None
    dockerfile: str | None = None
    vcs_repository: Annotated[str | None, pydantic.Field(alias="VCSRepository")] = None
    vcs_revision: Annotated[str | None, pydantic.Field(alias="VCSRevision")] = None
    target: str | None = None
    platform: list[str] | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    duration: int | None = None
    status: str | None = None
    error: BuildRecordError | None = None
    num_completed_steps: int | None = None
    num_total_steps: int | None = None
    num_cached_steps: int | None = None
    build_args: list[BuildRecordKeyValue] | None = None
    labels: list[BuildRecordKeyValue] | None = None
    config: BuildRecordConfig | None = None
    materials: list[BuildRecordMaterial] | None = None
    attachments: list[BuildRecordAttachment] | None = None
