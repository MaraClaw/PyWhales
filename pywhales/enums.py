"""Known Docker wire values and exact constrained command input types."""

from enum import StrEnum
from typing import Literal


class ContainerStatus(StrEnum):
    """Known lifecycle states reported for Docker containers."""

    CREATED = "created"
    RESTARTING = "restarting"
    RUNNING = "running"
    REMOVING = "removing"
    PAUSED = "paused"
    EXITED = "exited"
    DEAD = "dead"


class HealthStatus(StrEnum):
    """Known container health states, including absence of a health check."""

    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    NONE = "none"


class RestartPolicyName(StrEnum):
    """Known container restart-policy names used in Docker metadata."""

    NO = "no"
    ALWAYS = "always"
    ON_FAILURE = "on-failure"
    UNLESS_STOPPED = "unless-stopped"


class MountType(StrEnum):
    """Known mount source kinds reported by Docker."""

    BIND = "bind"
    VOLUME = "volume"
    TMPFS = "tmpfs"
    NPIPE = "npipe"
    CLUSTER = "cluster"
    IMAGE = "image"


class BindPropagation(StrEnum):
    """Known bind-mount propagation modes used by Docker."""

    PRIVATE = "private"
    RPRIVATE = "rprivate"
    SHARED = "shared"
    RSHARED = "rshared"
    SLAVE = "slave"
    RSLAVE = "rslave"


class Isolation(StrEnum):
    """Container isolation modes accepted by the constrained command inputs."""

    DEFAULT = "default"
    PROCESS = "process"
    HYPERV = "hyperv"


class TaskState(StrEnum):
    """Known lifecycle states reported for Swarm tasks."""

    NEW = "new"
    ALLOCATED = "allocated"
    PENDING = "pending"
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETE = "complete"
    SHUTDOWN = "shutdown"
    FAILED = "failed"
    REJECTED = "rejected"
    REMOVE = "remove"
    ORPHANED = "orphaned"


class RestartCondition(StrEnum):
    """Swarm restart conditions accepted by constrained command inputs."""

    NONE = "none"
    ON_FAILURE = "on-failure"
    ANY = "any"


class NodeRole(StrEnum):
    """Swarm node roles distinguishing workers from managers."""

    WORKER = "worker"
    MANAGER = "manager"


class NodeAvailability(StrEnum):
    """Known Swarm node scheduling-availability modes."""

    ACTIVE = "active"
    PAUSE = "pause"
    DRAIN = "drain"


class NodeState(StrEnum):
    """Known operational states reported for Swarm nodes."""

    UNKNOWN = "unknown"
    DOWN = "down"
    READY = "ready"
    DISCONNECTED = "disconnected"


class NodeReachability(StrEnum):
    """Known reachability states reported for Swarm managers."""

    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"
    REACHABLE = "reachable"


class EndpointMode(StrEnum):
    """Swarm service endpoint-discovery modes."""

    VIP = "vip"
    DNSRR = "dnsrr"


class PublishMode(StrEnum):
    """Swarm port-publication modes for ingress or direct host binding."""

    INGRESS = "ingress"
    HOST = "host"


class NetworkProtocol(StrEnum):
    """Known transport-protocol names in Docker port metadata."""

    TCP = "tcp"
    UDP = "udp"
    SCTP = "sctp"


class UpdateOrder(StrEnum):
    """Task replacement ordering for Swarm updates and rollbacks."""

    STOP_FIRST = "stop-first"
    START_FIRST = "start-first"


class FailureAction(StrEnum):
    """Known actions for handling Swarm service-update failures."""

    PAUSE = "pause"
    CONTINUE = "continue"
    ROLLBACK = "rollback"


class ServiceUpdateState(StrEnum):
    """Known progress states for Swarm service updates and rollbacks."""

    UPDATING = "updating"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLBACK_STARTED = "rollback_started"
    ROLLBACK_PAUSED = "rollback_paused"
    ROLLBACK_COMPLETED = "rollback_completed"


class ResourceScope(StrEnum):
    """Resource scopes accepted by constrained Docker command inputs."""

    SWARM = "swarm"
    GLOBAL = "global"
    LOCAL = "local"


class SwarmState(StrEnum):
    """Known local Swarm participation states reported by Docker."""

    INACTIVE = "inactive"
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    LOCKED = "locked"


class ComposeDependencyCondition(StrEnum):
    """Known conditions gating startup on a Compose dependency."""

    SERVICE_STARTED = "service_started"
    SERVICE_HEALTHY = "service_healthy"
    SERVICE_COMPLETED_SUCCESSFULLY = "service_completed_successfully"


class ComposeProgress(StrEnum):
    """Compose progress formats accepted by constrained command inputs."""

    AUTO = "auto"
    TTY = "tty"
    PLAIN = "plain"
    JSON = "json"
    QUIET = "quiet"


class ComposePullPolicy(StrEnum):
    """Known Compose pull policies, with command-specific input subsets."""

    ALWAYS = "always"
    MISSING = "missing"
    NEVER = "never"
    BUILD = "build"


class BuildxProgressMode(StrEnum):
    """Buildx progress-display modes represented by the input aliases."""

    AUTO = "auto"
    NONE = "none"
    PLAIN = "plain"
    QUIET = "quiet"
    RAWJSON = "rawjson"
    TTY = "tty"


class BuildxCallMethod(StrEnum):
    """Buildx frontend methods for builds and report-only calls."""

    BUILD = "build"
    CHECK = "check"
    OUTLINE = "outline"
    TARGETS = "targets"


class ClientType(StrEnum):
    """Explicit client behavior selections, independent of executable names."""

    DOCKER = "docker"
    NERDCTL = "nerdctl"
    UNKNOWN = "unknown"


class Orchestrator(StrEnum):
    """Known orchestrator selectors used by Docker stack operations."""

    SWARM = "swarm"
    KUBERNETES = "kubernetes"
    ALL = "all"


class ImageResolutionMode(StrEnum):
    """Image-resolution policies for Docker stack deployment."""

    ALWAYS = "always"
    CHANGED = "changed"
    NEVER = "never"


type ContainerStatusInput = (
    ContainerStatus
    | Literal[
        "created", "restarting", "running", "removing", "paused", "exited", "dead"
    ]
)
type HealthStatusInput = (
    HealthStatus | Literal["starting", "healthy", "unhealthy", "none"]
)
type IsolationInput = Isolation | Literal["default", "process", "hyperv"]
type ResourceScopeInput = ResourceScope | Literal["swarm", "global", "local"]
type RestartConditionInput = RestartCondition | Literal["none", "on-failure", "any"]
type NodeRoleInput = NodeRole | Literal["worker", "manager"]
type NodeAvailabilityInput = NodeAvailability | Literal["active", "pause", "drain"]
type EndpointModeInput = EndpointMode | Literal["vip", "dnsrr"]
type ImageResolutionModeInput = (
    ImageResolutionMode | Literal["always", "changed", "never"]
)
type UpdateOrderInput = UpdateOrder | Literal["stop-first", "start-first"]
type UpdateFailureActionInput = FailureAction | Literal["pause", "continue", "rollback"]
type RollbackFailureActionInput = Literal[
    "pause", "continue", FailureAction.PAUSE, FailureAction.CONTINUE
]
type ClientTypeInput = ClientType | Literal["docker", "nerdctl", "unknown"]
type ComposeProgressInput = (
    ComposeProgress | Literal["auto", "tty", "plain", "json", "quiet"]
)
type ComposeCreatePullPolicy = (
    ComposePullPolicy | Literal["always", "missing", "never", "build"]
)
type ComposeUpPullPolicy = Literal[
    "always",
    "missing",
    "never",
    ComposePullPolicy.ALWAYS,
    ComposePullPolicy.MISSING,
    ComposePullPolicy.NEVER,
]
type BuildxProgress = (
    BuildxProgressMode
    | Literal["auto", "none", "plain", "quiet", "rawjson", "tty", False]
)
type BuildxCall = BuildxCallMethod | Literal["build", "check", "outline", "targets"]
type BuildxBuildCall = Literal["build", BuildxCallMethod.BUILD]
type BuildxReportCall = Literal[
    "check",
    "outline",
    "targets",
    BuildxCallMethod.CHECK,
    BuildxCallMethod.OUTLINE,
    BuildxCallMethod.TARGETS,
]
