"""Bounded batch inspection behind the opt-in hydrated resource listings.

A hydrated listing costs one listing launch plus one inspect launch per chunk of
identities. Snapshots are matched by exact stored identity and pinned on the
returned handles. Resources that vanish between the listing and the inspect
are dropped by re-probing only the chunk whose inspect reported them missing.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pywhales.client_config import (
    ParsingError,
    ReloadableObject,
    ReloadableObjectFromJson,
)
from pywhales.exceptions import DockerException

HYDRATION_CHUNK_SIZE = 100
"""Most identities sent to one batch inspect launch."""


def hydrate_handles[HandleT: ReloadableObjectFromJson[Any]](
    handles: Sequence[HandleT],
    inspect_json: Callable[[Sequence[str]], str],
    missing: type[DockerException],
) -> list[HandleT]:
    """Pin an inspected snapshot on each handle that still exists.

    Args:
        handles: Distinct handles in listing order, each holding its full
            immutable identity without a snapshot.
        inspect_json: Run one batch inspect launch for the given identities and
            return its captured JSON text.
        missing: Exception class the inspect raises when an identity no longer
            exists.

    Returns:
        The handles that still exist, in their original order, each with a
        pinned snapshot from its own chunk's inspect.

    Raises:
        ParsingError: A chunk's response is malformed or its identities are not
            exactly the requested ones.
        DockerException: An inspect fails for a reason other than `missing`.
    """
    hydrated: list[HandleT] = []
    for start in range(0, len(handles), HYDRATION_CHUNK_SIZE):
        chunk = handles[start : start + HYDRATION_CHUNK_SIZE]
        hydrated.extend(_hydrate_chunk(chunk, inspect_json, missing))
    return hydrated


def _hydrate_chunk[HandleT: ReloadableObjectFromJson[Any]](
    chunk: Sequence[HandleT],
    inspect_json: Callable[[Sequence[str]], str],
    missing: type[DockerException],
) -> list[HandleT]:
    """Inspect one chunk, bisecting only after a missing-resource failure.

    Args:
        chunk: Distinct handles to inspect together.
        inspect_json: Run one batch inspect launch for the given identities.
        missing: Exception class signaling that an identity no longer exists.

    Returns:
        The surviving handles in order, pinned only after their chunk's whole
        response validated.
    """
    identities = [handle._get_immutable_id() for handle in chunk]
    try:
        json_str = inspect_json(identities)
    except missing:
        if len(chunk) == 1:
            return []
        middle = len(chunk) // 2
        return [
            *_hydrate_chunk(chunk[:middle], inspect_json, missing),
            *_hydrate_chunk(chunk[middle:], inspect_json, missing),
        ]
    snapshots = chunk[0]._parse_inspect_results(json_str, identities)
    id_attribute = chunk[0]._id_in_inspect
    by_identity = {getattr(snapshot, id_attribute): snapshot for snapshot in snapshots}
    if by_identity.keys() != set(identities):
        raise ParsingError("Hydrated inspect identities do not match the listing")
    for handle, identity in zip(chunk, identities, strict=True):
        handle._set_pinned_inspect_result(by_identity[identity])
    return list(chunk)


def release_pins(resources: Iterable[object]) -> None:
    """Expire the pinned snapshot of every resource handle among `resources`.

    Called after a mutating command, whether or not it succeeded, so no later
    read mistakes the pre-mutation listing for current state. Plain names and
    unpinned handles are left as they are.

    Args:
        resources: Command targets; strings and other non-handles are ignored.
    """
    for resource in resources:
        if isinstance(resource, ReloadableObject):
            resource._release_pin()
