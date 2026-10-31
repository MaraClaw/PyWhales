"""Preserve complete wire templates while validating required observation fields."""

import hashlib

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from pywhales._json import dumps_compat, loads_compat
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.convergence._types import Failure, ServiceTarget
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


class ContainerConfig(BaseModel):
    model_config = ConfigDict(strict=True)
    labels: ComposeLabels = Field(alias="Labels")
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


class ContainerWire(BaseModel):
    """Container identity, state and Compose labels; environment and command drop."""

    model_config = ConfigDict(strict=True)
    id: str = Field(alias="Id", min_length=1)
    state: ContainerState = Field(alias="State")
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
