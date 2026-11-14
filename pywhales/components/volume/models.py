"""Volume inspection snapshots whose mount paths refer to the selected daemon."""

import re
from datetime import datetime
from pathlib import Path

from pydantic import JsonValue, field_validator

from pywhales.enums import ResourceScope
from pywhales.utils import DockerCamelModel

_GO_TIME_STRING = re.compile(
    r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d+)?) ([+-]\d{2})(\d{2})(?: \S+)?"
)


class VolumeInspectResult(DockerCamelModel):
    name: str | None = None
    driver: str | None = None
    mountpoint: Path | None = None
    created_at: datetime | None = None
    status: dict[str, JsonValue] | None = None
    labels: dict[str, str] | None = None
    scope: ResourceScope | str | None = None
    options: dict[str, str] | None = None
    cluster_volume: dict[str, JsonValue] | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _accept_go_time_string(cls, value: object) -> object:
        """Rewrite Go's `time.Time.String()` form into RFC 3339.

        Local volumes report RFC 3339, but Swarm cluster volumes report values
        such as `2026-11-14 13:06:04.961510094 +0000 UTC`. Other values pass
        through unchanged.
        """
        if isinstance(value, str) and (match := _GO_TIME_STRING.fullmatch(value)):
            date, time, hours, minutes = match.groups()
            return f"{date}T{time}{hours}:{minutes}"
        return value

    @field_validator("mountpoint", mode="before")
    @classmethod
    def _empty_mountpoint_is_none(cls, value: object) -> object:
        """Map the empty mount point of a pending cluster volume to `None`."""
        return None if value == "" else value
