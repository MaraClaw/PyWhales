"""Typed local-image inspection snapshots, history layers, and search results.

Inspection snapshots reuse the shared container config models.
"""

from datetime import datetime
from typing import Annotated

from pydantic import Field, JsonValue, field_validator

import pywhales.components.container.models
from pywhales.utils import DockerCamelModel

MISSING_LAYER_ID = "<missing>"
"""Docker's placeholder for a history layer without a local image ID."""


class ImageHealthcheck(DockerCamelModel):
    test: list[str] | None = None
    interval: int | None = None
    timeout: int | None = None
    retries: int | None = None
    start_period: int | None = None


class ImageGraphDriver(DockerCamelModel):
    name: str | None = None
    data: JsonValue = None


class ImageRootFS(DockerCamelModel):
    type: str | None = None
    layers: list[str] | None = None
    base_layer: str | None = None


class ImageInspectResult(DockerCamelModel):
    id: str | None = None
    repo_tags: list[str] | None = None
    repo_digests: list[str] | None = None
    parent: str | None = None
    comment: str | None = None
    created: datetime | None = None
    container: str | None = None
    container_config: pywhales.components.container.models.ContainerConfig | None = None
    docker_version: str | None = None
    author: str | None = None
    config: pywhales.components.container.models.ContainerConfig | None = None
    architecture: str | None = None
    os: str | None = None
    os_version: str | None = None
    variant: str | None = None
    size: int | None = None
    virtual_size: int | None = None
    graph_driver: ImageGraphDriver | None = None
    root_fs: ImageRootFS | None = None
    metadata: dict[str, str] | None = None


class ImageHistoryLayer(DockerCamelModel):
    """One layer of an image's history, newest first as the CLI reports it.

    Attributes:
        id: Image ID that introduced the layer, or `None` when Docker reports
            the `<missing>` placeholder (layers pulled without their own image).
        created_at: Time the layer was created.
        created_by: Instruction that created the layer; may be empty.
        size: Layer size in bytes; zero for metadata-only instructions.
        comment: Layer comment; may be empty.
    """

    id: Annotated[str | None, Field(alias="ID")]
    created_at: datetime
    created_by: str
    size: int
    comment: str

    @field_validator("id", mode="before")
    @classmethod
    def _missing_id_is_none(cls, value: object) -> object:
        """Map Docker's `<missing>` placeholder to `None`."""
        return None if value == MISSING_LAYER_ID else value


class ImageSearchResult(DockerCamelModel):
    """One Docker Hub search result.

    Attributes:
        name: Repository name, including its namespace when it has one.
        description: Repository description; may be empty.
        is_official: Whether Docker Hub marks the repository as official.
        star_count: Number of stars the repository has.
    """

    name: str
    description: str
    is_official: bool
    star_count: int
