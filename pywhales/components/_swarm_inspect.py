"""Identity-based association and validation of plural Swarm inspect output."""

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Sequence
from typing import assert_never

from pywhales.client_config import ParsingError, ReloadableObjectFromJson
from pywhales.components.config.models import ConfigInspectResult
from pywhales.components.node.models import NodeInspectResult
from pywhales.components.secret.models import SecretInspectResult
from pywhales.components.service.models import ServiceInspectResult

type SwarmInspectResult = (
    ConfigInspectResult | NodeInspectResult | SecretInspectResult | ServiceInspectResult
)


def _inspect_name(result: SwarmInspectResult) -> str | None:
    match result:
        case NodeInspectResult(description=description):
            return description.hostname if description is not None else None
        case (
            ConfigInspectResult(spec=spec)
            | SecretInspectResult(spec=spec)
            | ServiceInspectResult(spec=spec)
        ):
            return spec.name if spec is not None else None
        case unreachable:
            assert_never(unreachable)


def _prefixed(ordered: list[str], prefix: str) -> list[str]:
    """Return the identities in sorted ``ordered`` that start with ``prefix``."""
    width = len(prefix)

    def head(identity: str) -> str:
        return identity[:width]

    start = bisect_left(ordered, prefix, key=head)
    return ordered[start : bisect_right(ordered, prefix, lo=start, key=head)]


def parse_swarm_inspect_results[InspectResultT: SwarmInspectResult](
    parser: ReloadableObjectFromJson[InspectResultT],
    json_str: str,
    references: Sequence[str],
) -> list[tuple[str, InspectResultT]]:
    """Validate and associate Swarm inspection results in request order.

    Unique slot labels keep the shared parser's positional duplicate check from
    associating repeated request references with unrelated reordered responses.
    Identity consistency and multiplicity are checked after reference resolution.

    Args:
        parser: Resource handle supplying the response model and diagnostic policy.
        json_str: Captured JSON array from one plural Swarm inspect command.
        references: Requested IDs, names, or ID prefixes, including duplicates.

    Returns:
        Identity and snapshot pairs in request order. Exact IDs take precedence
        over exact names (hostnames for nodes), then unique ID prefixes.

    Raises:
        ParsingError: If response shape, identities, reference resolution, snapshot
            consistency, or requested multiplicity is invalid.
    """
    results = parser._parse_inspect_results(
        json_str, [str(index) for index in range(len(references))]
    )
    return associate_swarm_inspect_results(
        results, references, raw_diagnostics=parser.client_config.raw_diagnostics
    )


def associate_swarm_inspect_results[InspectResultT: SwarmInspectResult](
    results: Sequence[InspectResultT],
    references: Sequence[str],
    *,
    raw_diagnostics: bool = False,
) -> list[tuple[str, InspectResultT]]:
    """Associate parsed Swarm models with references using whole-batch rules.

    References resolve through indexes built once per call, never by scanning every
    snapshot per reference, and the name and prefix indexes only when an exact ID
    misses. Match counts are distinct identities.

    Args:
        results: Parsed models of one or more launches for the same references.
        references: Requested IDs, names, or ID prefixes, including duplicates.
        raw_diagnostics: Whether error text may include identities and references.

    Returns:
        Identity and snapshot pairs in request order. Exact IDs take precedence
        over exact names (hostnames for nodes), then unique ID prefixes.

    Raises:
        ParsingError: If an identity is missing, one identity has differing
            snapshots, the first unresolved reference in request order matches
            other than one identity, or resolved multiplicities differ.
    """
    snapshots: dict[str, InspectResultT] = {}
    identities: list[str] = []
    for result in results:
        identity = result.id
        if identity is None or identity == "":
            raise ParsingError("Missing Swarm inspect identity")
        if identity in snapshots and snapshots[identity] != result:
            raise ParsingError(
                f"Inconsistent inspect snapshot for {identity!r}"
                if raw_diagnostics
                else "Inconsistent inspect snapshot"
            )
        snapshots[identity] = result
        identities.append(identity)

    names: dict[str, list[str]] | None = None
    ordered: list[str] = []
    resolved: list[tuple[str, InspectResultT]] = []
    for reference in references:
        if reference in snapshots:
            resolved.append((reference, snapshots[reference]))
            continue
        matches: Sequence[str] = ()
        if reference:
            if names is None:
                names = {}
                for identity, result in snapshots.items():
                    name = _inspect_name(result)
                    if name is not None:
                        names.setdefault(name, []).append(identity)
                ordered = sorted(snapshots)
            matches = names.get(reference) or _prefixed(ordered, reference)
        if len(matches) != 1:
            raise ParsingError(
                f"Expected one inspect identity for {reference!r}, received {len(matches)}"
                if raw_diagnostics
                else f"Expected one inspect identity for the requested reference, received {len(matches)}"
            )
        resolved.append((matches[0], snapshots[matches[0]]))
    if Counter(identity for identity, _ in resolved) != Counter(identities):
        raise ParsingError("Inspect identities do not match requested multiplicities")
    return resolved
