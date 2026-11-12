"""Bounded batch inspection behind the opt-in hydrated resource listings.

A hydrated listing costs one listing launch plus one inspect launch per chunk of
identities. Snapshots are matched by exact stored identity and pinned on the
returned handles. Resources that vanish between the listing and the inspect
are dropped by re-probing only the chunk whose inspect reported them missing.
A listing that prints truncated IDs first resolves them to full identities the
same way.
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


def resolve_identities(
    references: Sequence[str],
    resolve_output: Callable[[Sequence[str]], str],
    missing: type[DockerException],
) -> list[str]:
    """Expand abbreviated references to full identities, dropping vanished ones.

    For a listing that prints truncated IDs. References go out in chunks of at
    most `HYDRATION_CHUNK_SIZE`; a chunk whose launch raises `missing` is
    bisected, so only its own members are probed again.

    Args:
        references: Distinct abbreviated identities in listing order.
        resolve_output: Run one launch that prints the full identity of each
            given reference on its own line, in request order.
        missing: Exception class the launch raises when a reference no longer
            exists.

    Returns:
        The full identities of the references that still exist, in listing order.

    Raises:
        ParsingError: A response does not hold exactly one identity per requested
            reference, each beginning with that reference.
        DockerException: A launch fails for a reason other than `missing`.
    """
    resolved: list[str] = []
    for start in range(0, len(references), HYDRATION_CHUNK_SIZE):
        chunk = references[start : start + HYDRATION_CHUNK_SIZE]
        resolved.extend(_resolve_chunk(chunk, resolve_output, missing))
    return resolved


def _resolve_chunk(
    chunk: Sequence[str],
    resolve_output: Callable[[Sequence[str]], str],
    missing: type[DockerException],
) -> list[str]:
    """Resolve one chunk, bisecting only after a missing-resource failure.

    Args:
        chunk: Distinct abbreviated identities to resolve together.
        resolve_output: Run one launch printing a full identity per reference.
        missing: Exception class signaling that a reference no longer exists.

    Returns:
        The surviving full identities in order, accepted only when the whole
        response matched the chunk.
    """
    try:
        output = resolve_output(chunk)
    except missing:
        if len(chunk) == 1:
            return []
        middle = len(chunk) // 2
        return [
            *_resolve_chunk(chunk[:middle], resolve_output, missing),
            *_resolve_chunk(chunk[middle:], resolve_output, missing),
        ]
    identities = output.splitlines()
    if len(identities) != len(chunk) or not all(
        identity.startswith(reference)
        for identity, reference in zip(identities, chunk, strict=True)
    ):
        raise ParsingError("Resolved identities do not match the listing")
    return identities


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
