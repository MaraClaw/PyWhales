"""Preserve complete wire templates while validating required observation fields."""

import hashlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from pywhales._json import dumps_compat, loads_compat
from pywhales._observation import check_observation, get_observation
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.convergence._types import Failure, ServiceTarget
from pywhales.exceptions import (
    DockerException,
    ObservationCancelled,
    ObservationTimeout,
)
from pywhales.utils import run


class Version(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int = Field(alias="Index", ge=0)


class JobIteration(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int = Field(default=0, alias="Index", ge=0)


class JobStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    iteration: JobIteration = Field(alias="JobIteration")


class Spec(BaseModel):
    model_config = ConfigDict(strict=True)
    mode: dict[str, JsonValue] = Field(alias="Mode")
    template: dict[str, JsonValue] = Field(alias="TaskTemplate")


class UpdateStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    state: str = Field(alias="State")


class ServiceWire(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(alias="ID", min_length=1)
    version: Version = Field(alias="Version")
    spec: Spec = Field(alias="Spec")
    update: UpdateStatus | None = Field(default=None, alias="UpdateStatus")
    job_status: JobStatus | None = Field(default=None, alias="JobStatus")


class TaskStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    state: str = Field(alias="State", min_length=1)


class TaskWire(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(alias="ID", min_length=1)
    service_id: str = Field(alias="ServiceID", min_length=1)
    slot: int | None = Field(default=None, alias="Slot")
    node_id: str = Field(default="", alias="NodeID")
    desired: str = Field(alias="DesiredState", min_length=1)
    status: TaskStatus = Field(alias="Status")
    spec: dict[str, JsonValue] | None = Field(default=None, alias="Spec")
    job_iteration: JobIteration | None = Field(default=None, alias="JobIteration")


class NodeSpec(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["worker", "manager"] = Field(alias="Role")
    availability: Literal["active", "pause", "drain"] = Field(alias="Availability")


class NodeStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    state: Literal["unknown", "down", "ready", "disconnected"] = Field(alias="State")


class NodeManagerStatus(BaseModel):
    model_config = ConfigDict(strict=True)
    reachability: Literal["unknown", "unreachable", "reachable"] = Field(
        alias="Reachability"
    )
    leader: JsonValue = Field(default=False, alias="Leader")


class NodeWire(BaseModel):
    """Only the fields needed to evaluate an immutable node identity."""

    model_config = ConfigDict(strict=True)
    id: str = Field(alias="ID", min_length=1)
    spec: NodeSpec = Field(alias="Spec")
    status: NodeStatus = Field(alias="Status")
    manager_status: NodeManagerStatus | None = Field(
        default=None, alias="ManagerStatus"
    )


class NodeMissing(Exception):
    """An exact missing-node response retaining its original CLI exception."""

    def __init__(self, cause: DockerException) -> None:
        self.cause = cause
        super().__init__("The selected node is missing.")


class ContainerHealth(BaseModel):
    model_config = ConfigDict(strict=True)
    status: str = Field(alias="Status", min_length=1)


class ContainerState(BaseModel):
    model_config = ConfigDict(strict=True)
    status: str = Field(alias="Status", min_length=1)
    restarting: bool = Field(alias="Restarting")
    paused: bool = Field(alias="Paused")
    exit_code: int = Field(alias="ExitCode")
    health: ContainerHealth | None = Field(default=None, alias="Health")


class ComposeLabels(BaseModel):
    """Only the Compose labels evaluation needs; every other label is dropped."""

    model_config = ConfigDict(strict=True)
    service: str = Field(alias="com.docker.compose.service", min_length=1)
    config_hash: str | None = Field(
        default=None, alias="com.docker.compose.config-hash"
    )
    oneoff: str | None = Field(default=None, alias="com.docker.compose.oneoff")


class StandaloneConfig(BaseModel):
    model_config = ConfigDict(strict=True)
    healthcheck: bool = Field(default=False, alias="Healthcheck")

    @field_validator("healthcheck", mode="before")
    @classmethod
    def configured_healthcheck(cls, value: object) -> bool:
        """Retain only check presence, never its command or environment values."""
        if value is None:
            return False
        check = TypeAdapter(dict[str, JsonValue]).validate_python(value, strict=True)
        test = TypeAdapter(list[str]).validate_python(
            check.get("Test", []), strict=True
        )
        return bool(test) and test[0] != "NONE"


class ContainerConfig(StandaloneConfig):
    labels: ComposeLabels = Field(alias="Labels")


class ContainerWireBase(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(alias="Id", min_length=1)
    state: ContainerState = Field(alias="State")
    restart_count: JsonValue = Field(default=None, alias="RestartCount")


class StandaloneContainerWire(ContainerWireBase):
    """Only the identity and state required to evaluate a standalone container."""

    id: str = Field(alias="Id", pattern=r"^[0-9a-f]{64}$")
    name: str | None = Field(default=None, alias="Name")
    config: StandaloneConfig = Field(alias="Config")


class ContainerWire(ContainerWireBase):
    """Container identity, state, labels and raw restart count; env and command drop.

    ``restart_count`` is not validated here, so a disabled restart budget keeps
    ignoring whatever Docker reports; the evaluator checks it when enabled.
    """

    config: ContainerConfig = Field(alias="Config")


def canonical(value: dict[str, JsonValue], *, template: bool = False) -> str:
    """Serialize a complete JSON object, normalizing only the default runtime."""
    normalized = dict(value)
    if template and normalized.get("Runtime") == "container":
        del normalized["Runtime"]
    return dumps_compat(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(service: ServiceWire) -> str:
    """Fingerprint the complete mode and normalized task template."""
    template = canonical(service.spec.template, template=True)
    generation = canonical(
        {"Mode": service.spec.mode, "TaskTemplate": loads_compat(template)}
    )
    return hashlib.sha256(generation.encode()).hexdigest()


def same_generation(target: ServiceTarget, service: ServiceWire) -> bool:
    """Compare the generation fence appropriate to the captured mode."""
    if fingerprint(service) != target.fingerprint:
        return False
    if target.mode in {"replicated_job", "global_job"}:
        return (
            service.job_status is not None
            and service.job_status.iteration.index == target.job_iteration
        )
    return service.version.index == target.version


def inspect_service(config: ClientConfig, service_id: str) -> ServiceWire:
    """Inspect exactly one immutable service ID."""
    payload = run([*config.docker_cmd, "service", "inspect", service_id])
    results = TypeAdapter(list[ServiceWire]).validate_json(payload)
    if len(results) != 1 or results[0].id != service_id:
        raise ParsingError("Service inspect identity mismatch")
    return results[0]


def inspect_node(config: ClientConfig, node_id: str) -> NodeWire:
    """Inspect an exact ID, distinguishing only its exact missing-node response."""
    try:
        payload = run([*config.docker_cmd, "node", "inspect", node_id])
    except DockerException as error:
        if (
            type(error) is DockerException
            and error.stderr
            == f"Error response from daemon: node {node_id} not found\n"
        ):
            raise NodeMissing(error) from error
        raise
    results = TypeAdapter(list[NodeWire]).validate_json(payload)
    if len(results) != 1 or results[0].id != node_id:
        raise ParsingError("Node inspect identity mismatch")
    return results[0]


def inspect_task(config: ClientConfig, task_id: str) -> TaskWire:
    """Inspect exactly one listed immutable task ID."""
    payload = run([*config.docker_cmd, "inspect", "--type", "task", task_id])
    results = TypeAdapter(list[TaskWire]).validate_json(payload)
    if len(results) != 1 or results[0].id != task_id:
        raise ParsingError("Task inspect identity mismatch")
    return results[0]


def list_compose_ids(config: ClientConfig, services: tuple[str, ...]) -> list[str]:
    """List the project's container IDs for the selected services, stopped included."""
    output = run([*config.get_compose_command(), "ps", "--all", "--quiet", *services])
    identities = output.splitlines()
    if any(not identity for identity in identities) or len(identities) != len(
        set(identities)
    ):
        raise ParsingError("Invalid container listing")
    return identities


def inspect_container(config: ClientConfig, container_id: str) -> ContainerWire:
    """Inspect exactly one listed container, requiring its full ID in the reply."""
    payload = run([*config.docker_cmd, "inspect", "--type", "container", container_id])
    results = TypeAdapter(list[ContainerWire]).validate_json(payload)
    if len(results) != 1 or results[0].id != container_id:
        raise ParsingError("Container inspect identity mismatch")
    return results[0]


def inspect_standalone(config: ClientConfig, reference: str) -> StandaloneContainerWire:
    """Inspect exactly one container without requiring Compose labels."""
    payload = run([*config.docker_cmd, "inspect", "--type", "container", reference])
    results = TypeAdapter(list[StandaloneContainerWire]).validate_json(payload)
    if len(results) != 1:
        raise ParsingError("Container inspect identity mismatch")
    return results[0]


@dataclass(frozen=True, slots=True)
class _Kind[Wire: BaseModel]:
    """One batchable inspection: its command, wire model, ID key and single probe."""

    command: tuple[str, ...]
    model: type[Wire]
    key: str
    single: Callable[[ClientConfig, str], Wire]


_TASKS = _Kind(("inspect", "--type", "task"), TaskWire, "ID", inspect_task)
_CONTAINERS = _Kind(
    ("inspect", "--type", "container"), ContainerWire, "Id", inspect_container
)
_STANDALONES = _Kind(
    ("inspect", "--type", "container"),
    StandaloneContainerWire,
    "Id",
    inspect_standalone,
)
_SERVICES = _Kind(("service", "inspect"), ServiceWire, "ID", inspect_service)
_NODES = _Kind(("node", "inspect"), NodeWire, "ID", inspect_node)


def _batch_items[Wire: BaseModel](
    config: ClientConfig, kind: _Kind[Wire], chunk: Sequence[str]
) -> list[dict[str, JsonValue]]:
    """Inspect a chunk in one launch and return raw items in request order.

    Items are mapped by their ``kind.key`` value, never by position, and that set
    must equal the requested IDs exactly.

    Raises:
        ParsingError: If the reply's IDs are not exactly the requested IDs.
    """
    payload = run([*config.docker_cmd, *kind.command, *chunk])
    by_id: dict[str, dict[str, JsonValue]] = {}
    for item in TypeAdapter(list[dict[str, JsonValue]]).validate_json(payload):
        identity = item.get(kind.key)
        if type(identity) is not str or identity in by_id:
            raise ParsingError("Batch inspect identity mismatch")
        by_id[identity] = item
    if by_id.keys() != set(chunk) or len(by_id) != len(chunk):
        raise ParsingError("Batch inspect identity mismatch")
    return [by_id[identity] for identity in chunk]


def _batch_prefix[Wire: BaseModel](
    config: ClientConfig, kind: _Kind[Wire], chunk: Sequence[str]
) -> Iterator[Wire]:
    """Yield one batch's validated items in request order, stopping at any failure.

    A failed launch or reply yields nothing. A malformed item ends the iteration
    before itself, so earlier items are still delivered and the caller re-probes the
    rest singly. Cancellation and deadline propagate; they would recur on re-probe.
    """
    try:
        items = _batch_items(config, kind, chunk)
    except ObservationCancelled, ObservationTimeout:
        raise
    except Exception:
        return
    for item in items:
        try:
            wire = kind.model.model_validate(item)
        except ValidationError:
            return
        yield wire


def _inspect_many[Wire: BaseModel](
    config: ClientConfig, kind: _Kind[Wire], identities: Sequence[str]
) -> Iterator[Wire]:
    """Inspect exact IDs in policy-sized chunks, yielding wires in request order.

    A chunk is one multi-ID launch, sized by the active policy's ``max_references``
    (one outside an observation) and preceded by an observation checkpoint. The
    batch never explains its own failure: whatever the batch does not deliver,
    including everything after a failure, is re-probed one ID at a time in order,
    so the first failure, its type and its cause match a purely single-ID walk.
    Nothing launches before iteration, and unread chunks never launch.
    """
    state = get_observation()
    size = 1 if state is None else state.policy.max_references
    for start in range(0, len(identities), size):
        chunk = identities[start : start + size]
        delivered = 0
        if len(chunk) > 1:
            check_observation()
            for wire in _batch_prefix(config, kind, chunk):
                delivered += 1
                yield wire
        for identity in chunk[delivered:]:
            check_observation()
            yield kind.single(config, identity)


def inspect_tasks(config: ClientConfig, task_ids: Sequence[str]) -> Iterator[TaskWire]:
    """Return a lazy iterator over exact task IDs, inspected in chunks.

    Args:
        config: Client settings for the manager.
        task_ids: Distinct full task IDs, in listing order.

    Returns:
        An iterator yielding each task in ``task_ids`` order. Launches happen as it
        is consumed, and it raises what single-ID inspection would raise.
    """
    return _inspect_many(config, _TASKS, task_ids)


def inspect_containers(
    config: ClientConfig, container_ids: Sequence[str]
) -> Iterator[ContainerWire]:
    """Return a lazy iterator over exact Compose container IDs, inspected in chunks.

    Args:
        config: Client settings for the Docker daemon.
        container_ids: Distinct full container IDs, in listing order.

    Returns:
        An iterator yielding each container in ``container_ids`` order, raising what
        single-ID inspection would raise.
    """
    return _inspect_many(config, _CONTAINERS, container_ids)


def inspect_standalones(
    config: ClientConfig, container_ids: Sequence[str]
) -> Iterator[StandaloneContainerWire]:
    """Return a lazy iterator over fixed standalone container IDs, in chunks.

    Args:
        config: Client settings for the Docker daemon.
        container_ids: Distinct full container IDs, in target order.

    Returns:
        An iterator yielding each container in ``container_ids`` order, raising what
        single-ID inspection would raise. A single probe may return a different
        ID, which the caller must compare.
    """
    return _inspect_many(config, _STANDALONES, container_ids)


def inspect_services(targets: Sequence[ServiceTarget]) -> Iterator[ServiceWire]:
    """Inspect each captured service in chunks, yielding wires in target order.

    A chunk never spans two ``client_config`` objects, compared by identity: a run of
    consecutive targets sharing one object is chunked together, so order is kept even
    when a stack mixes configs. Nothing launches before iteration, and a failed batch
    re-probes singly as ``inspect_tasks`` does.

    Args:
        targets: Captured services with distinct IDs, in evaluation order.

    Yields:
        Each target's current service, raising what single-ID inspection would raise.
    """
    for _, run_ in groupby(targets, key=lambda target: id(target.client_config)):
        members = list(run_)
        yield from _inspect_many(
            members[0].client_config,
            _SERVICES,
            [member.service_id for member in members],
        )


def inspect_nodes(config: ClientConfig, node_ids: Sequence[str]) -> Iterator[NodeWire]:
    """Return a lazy iterator over fixed node IDs, inspected in chunks.

    Args:
        config: Client settings for the manager.
        node_ids: Distinct exact node IDs, in target order.

    Returns:
        An iterator yielding each node in ``node_ids`` order. It raises what
        single-ID inspection would raise, so ``NodeMissing`` comes only from a
        single probe's exact missing-node response, never from a batch.
    """
    return _inspect_many(config, _NODES, node_ids)


def resolve_container(config: ClientConfig, reference: str) -> StandaloneContainerWire:
    """Resolve a name or ID prefix to one matching container projection."""
    result = inspect_standalone(config, reference)
    if not result.id.startswith(reference) and (
        result.name is None
        or result.name.removeprefix("/") != reference.removeprefix("/")
    ):
        raise ParsingError("Container inspect identity mismatch")
    return result


def target_from_wire(
    config: ClientConfig,
    service: ServiceWire,
    approved_node_ids: tuple[str, ...] | None = None,
) -> ServiceTarget | Failure:
    """Capture a supported generation, or reject an unsupported mode."""
    mode = service.spec.mode
    total_completions = 0
    job_iteration = None
    restart_condition = None
    if set(mode) == {"Replicated"} and isinstance(mode["Replicated"], dict):
        replicated = mode["Replicated"]
        replicas = replicated.get("Replicas")
        if (
            set(replicated) != {"Replicas"}
            or type(replicas) is not int
            or replicas < 0
            or approved_node_ids is not None
        ):
            return Failure(reason="unsupported", service_id=service.id)
        captured_mode = "replicated"
    elif (
        set(mode) == {"Global"}
        and isinstance(mode["Global"], dict)
        and approved_node_ids is not None
    ):
        replicas = 0
        captured_mode = "global"
    elif set(mode) == {"ReplicatedJob"} and isinstance(mode["ReplicatedJob"], dict):
        job = mode["ReplicatedJob"]
        maximum = job.get("MaxConcurrent", 1)
        total = job.get("TotalCompletions", maximum)
        if (
            set(job) - {"MaxConcurrent", "TotalCompletions"}
            or type(maximum) is not int
            or maximum < 1
            or type(total) is not int
            or total < 1
            or approved_node_ids is not None
        ):
            return Failure(reason="unsupported", service_id=service.id)
        replicas = 0
        total_completions = total
        captured_mode = "replicated_job"
    elif (
        set(mode) == {"GlobalJob"}
        and mode["GlobalJob"] == {}
        and approved_node_ids is not None
    ):
        replicas = 0
        captured_mode = "global_job"
    else:
        return Failure(reason="unsupported", service_id=service.id)
    template = service.spec.template
    if captured_mode in {"replicated_job", "global_job"}:
        if service.job_status is None:
            return Failure(reason="malformed", service_id=service.id)
        job_iteration = service.job_status.iteration.index
        restart = template.get("RestartPolicy")
        condition = restart.get("Condition") if isinstance(restart, dict) else None
        if isinstance(condition, str):
            restart_condition = condition
    container = template.get("ContainerSpec")
    image = container.get("Image") if isinstance(container, dict) else None
    if not isinstance(image, str) or image == "":
        return Failure(reason="malformed", service_id=service.id)
    if template.get("Runtime", "container") != "container":
        return Failure(reason="unsupported", service_id=service.id)
    template_json = canonical(template, template=True)
    return ServiceTarget(
        client_config=config,
        service_id=service.id,
        version=service.version.index,
        fingerprint=fingerprint(service),
        template=template_json,
        replicas=replicas,
        mode=captured_mode,
        approved_node_ids=approved_node_ids,
        job_iteration=job_iteration,
        total_completions=total_completions,
        restart_condition=restart_condition,
    )
