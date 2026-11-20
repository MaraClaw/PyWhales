"""Ordered, bounded resource inspection without publishing original caches."""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import get_args

from pywhales._bulk_reload import _identity
from pywhales._environment import (
    finalize_environment,
    frozen_environment_scope,
    resolve_environment,
)
from pywhales._execution_policy import get_execution_policy
from pywhales._inventory_dispatch import inspect_group, make_resource, resource_kind
from pywhales._launch_limits import LaunchMeasurement, LaunchPlanner
from pywhales._observation import check_observation, observation
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components._swarm_inspect import (
    SwarmInspectResult,
    associate_swarm_inspect_results,
)
from pywhales.components.config.models import ConfigInspectResult
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.components.node.models import NodeInspectResult
from pywhales.components.secret.models import SecretInspectResult
from pywhales.components.service.models import ServiceInspectResult
from pywhales.exceptions import ObservationLimitError
from pywhales.inventory import (
    InspectionPolicy,
    InventoryKind,
    InventoryPayload,
    InventoryRecord,
    InventoryRequest,
    InventoryResource,
)


@dataclass(frozen=True, slots=True)
class _Occurrence:
    index: int
    reference: str
    exact: bool
    insecure: bool = False


@dataclass(frozen=True, slots=True)
class _Group:
    kind: InventoryKind
    config: ClientConfig
    occurrences: list[_Occurrence]


def _normalize(items: Sequence[InventoryRequest | InventoryResource]) -> list[_Group]:
    groups: dict[tuple[int, InventoryKind], _Group] = {}
    index = 0
    for item in items:
        if isinstance(item, InventoryRequest):
            if item.kind not in get_args(InventoryKind.__value__):
                raise ValueError("Unsupported inventory kind")
            if not isinstance(item.client_config, ClientConfig):
                raise TypeError("Invalid client configuration")
            if (
                not isinstance(item.references, tuple)
                or not item.references
                or not all(
                    isinstance(reference, str) and reference
                    for reference in item.references
                )
            ):
                raise ValueError("Inventory references must be nonempty strings")
            kind, config, references, exact, insecure = (
                item.kind,
                item.client_config,
                item.references,
                False,
                False,
            )
        elif isinstance(item, InventoryResource.__value__):
            identity = _identity(item)
            if not isinstance(identity, str) or not identity:
                raise ParsingError("Missing immutable inspect identity")
            kind, config, references, exact, insecure = (
                resource_kind(item),
                item.client_config,
                (identity,),
                True,
                isinstance(item, ManifestList) and item.insecure,
            )
        else:
            raise TypeError("Unsupported inventory item")
        group = groups.setdefault((id(config), kind), _Group(kind, config, []))
        for reference in references:
            group.occurrences.append(_Occurrence(index, reference, exact, insecure))
            index += 1
    return list(groups.values())


def _prefix(group: _Group, arguments: Sequence[object]) -> list[str]:
    command = [str(argument) for argument in arguments]
    if group.kind == "builder":
        return [*command, "buildx", "ls", "--format", "{{json . }}"]
    if group.kind == "task":
        return [*command, "inspect", "--type", "task"]
    if group.kind == "manifest_list":
        return [*command, "manifest", "inspect"]
    return [*command, group.kind, "inspect"]


def _chunks(
    group: _Group, measurement: LaunchMeasurement, max_references: int
) -> list[list[_Occurrence]]:
    """Plan one group's launches, each checked against the measured prefix.

    Raises:
        ObservationLimitError: If the builder listing, or a launch holding a single
            reference, exceeds a limit; the first such reference in order fails.
    """
    if group.kind == "builder":
        measurement.check()
        return [group.occurrences]
    if group.kind == "manifest_list":
        # One list per launch: a second positional would name a manifest inside it.
        for occurrence in group.occurrences:
            measurement.reset()
            measurement.append(occurrence.reference)
            if occurrence.insecure:
                measurement.append("--insecure")
            measurement.check()
        return [[occurrence] for occurrence in group.occurrences]
    references = [occurrence.reference for occurrence in group.occurrences]
    return [
        group.occurrences[part]
        for part in measurement.chunks(references, max_references)
    ]


def _record(
    group: _Group, identity: str, resource: InventoryResource, payload: InventoryPayload
) -> InventoryRecord:
    """Return a record holding a deep copy of the staged snapshot."""
    return InventoryRecord(
        kind=group.kind, id=identity, payload=payload.model_copy(deep=True)
    )


def _handle(
    group: _Group, identity: str, resource: InventoryResource, payload: InventoryPayload
) -> InventoryResource:
    """Return a deep copy of the staged handle that keeps its client configuration."""
    return deepcopy(resource, {id(group.config): group.config})


def _stage[Detached](
    items: Sequence[InventoryRequest | InventoryResource],
    policy: InspectionPolicy,
    detach: Callable[[_Group, str, InventoryResource, InventoryPayload], Detached],
) -> list[Detached]:
    """Inspect every item and return one detached value per reference in order.

    Args:
        items: Requests and handles; references keep their input order.
        policy: Limits for the observation enclosing planning and every launch.
        detach: Builds each returned value from a validated staged handle and its
            snapshot; staging keeps neither once it returns.

    Returns:
        One value per reference, including duplicates, in input order.
    """
    with observation(policy) as state:
        limit = state.policy.max_total_references
        if limit is not None:
            count = 0
            for item in items:
                count += (
                    len(item.references) if isinstance(item, InventoryRequest) else 1
                )
                if count > limit:
                    raise ObservationLimitError("total_references", limit, count)
        groups = _normalize(items)
        planner = LaunchPlanner(state.policy)
        plans: list[tuple[_Group, Mapping[str, str], list[list[_Occurrence]]]] = []
        for group in groups:
            check_observation(state)
            command = group.config.docker_cmd
            try:
                execution_policy = get_execution_policy(command)
                environment = resolve_environment(
                    execution_policy.environment,
                    frozen=execution_policy.frozen_environment,
                )
            except TypeError, ValueError:
                raise ObservationLimitError("environment", None, None) from None
            prefix = _prefix(group, command)
            environment = finalize_environment(environment, prefix)
            measurement = planner.measure(prefix, environment)
            plans.append(
                (
                    group,
                    environment,
                    _chunks(group, measurement, state.policy.max_references),
                )
            )
        state.check_launch_attempts(sum(len(chunks) for _, _, chunks in plans))
        detached: dict[int, Detached] = {}
        for group, environment, chunks in plans:
            aliases: dict[str, str] = {}
            snapshots = {}
            swarm_models: list[SwarmInspectResult] = []
            group_ids: list[str] = []
            for chunk in chunks:
                check_observation(state)
                request = [
                    make_resource(
                        group.kind,
                        group.config,
                        entry.reference,
                        insecure=entry.insecure,
                    )
                    for entry in chunk
                ]
                with frozen_environment_scope(group.config, environment):
                    inspected = inspect_group(request)
                if len(inspected) != len(chunk):
                    raise ParsingError("Unexpected inspect result count")
                for entry, resource in zip(chunk, inspected, strict=True):
                    if resource_kind(resource) != group.kind:
                        raise TypeError("Inspect returned an unexpected resource kind")
                    identity = _identity(resource)
                    payload = resource._inspect_result
                    if (
                        payload is None
                        or getattr(payload, resource._id_in_inspect) != identity
                    ):
                        raise ParsingError(
                            "Missing or inconsistent staged inspect identity"
                        )
                    if entry.exact and entry.reference != identity:
                        raise ParsingError(
                            "Inspect identity does not match immutable resource identity"
                        )
                    if (
                        entry.reference in aliases
                        and aliases[entry.reference] != identity
                    ):
                        raise ParsingError("Inconsistent inspect identity")
                    if identity in snapshots and snapshots[identity] != payload:
                        raise ParsingError("Inconsistent inspect snapshot")
                    aliases[entry.reference] = identity
                    snapshots[identity] = payload
                    group_ids.append(identity)
                    if isinstance(
                        payload,
                        ConfigInspectResult
                        | NodeInspectResult
                        | SecretInspectResult
                        | ServiceInspectResult,
                    ):
                        swarm_models.append(payload)
                    detached[entry.index] = detach(group, identity, resource, payload)
            if swarm_models:
                resolved = associate_swarm_inspect_results(
                    swarm_models,
                    [entry.reference for entry in group.occurrences],
                    raw_diagnostics=group.config.raw_diagnostics,
                )
                if [identity for identity, _ in resolved] != group_ids:
                    raise ParsingError("Inspect associations differ across chunks")
        check_observation(state)
        return [detached[index] for index in range(len(detached))]


def stage_records(
    items: Sequence[InventoryRequest | InventoryResource], policy: InspectionPolicy
) -> list[InventoryRecord]:
    """Return detached records in input order without changing input handles."""
    return _stage(items, policy, _record)


def stage_resources(
    resources: Sequence[InventoryResource], policy: InspectionPolicy
) -> list[InventoryResource]:
    """Return newly hydrated handles in input order without changing originals."""
    return _stage(resources, policy, _handle)
