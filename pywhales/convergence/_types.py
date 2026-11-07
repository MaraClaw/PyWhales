"""Immutable convergence targets and caller-visible outcomes."""

from dataclasses import dataclass, field
from typing import Literal

from pywhales.client_config import ClientConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceTarget:
    """A captured service generation and its originating client configuration."""

    client_config: ClientConfig = field(repr=False, compare=False)
    service_id: str
    version: int
    fingerprint: str
    template: str = field(repr=False)
    replicas: int
    mode: Literal["replicated", "global", "replicated_job", "global_job"] = "replicated"
    approved_node_ids: tuple[str, ...] | None = field(default=None, repr=False)
    job_iteration: int | None = None
    total_completions: int = 0
    restart_condition: str | None = None

    def __post_init__(self) -> None:
        """Reject invalid or mode-inconsistent node membership."""
        nodes = self.approved_node_ids
        if self.mode in {"replicated", "replicated_job"} and nodes is None:
            return
        if (
            self.mode not in {"global", "global_job"}
            or type(nodes) is not tuple
            or not nodes
            or any(type(node) is not str or not node for node in nodes)
            or len(set(nodes)) != len(nodes)
        ):
            raise ValueError("Global targets require distinct nonempty node IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class StackTarget:
    """An explicit, nonempty and fixed set of service generations."""

    services: tuple[ServiceTarget, ...]

    def __post_init__(self) -> None:
        """Reject empty or duplicate membership."""
        if (
            type(self.services) is not tuple
            or not self.services
            or len({target.service_id for target in self.services})
            != len(self.services)
        ):
            raise ValueError("Stack target must contain distinct services")


@dataclass(frozen=True, slots=True, kw_only=True)
class ComposeService:
    """One selected Compose service, its config hash and its full container IDs.

    A completion service is expected to exit 0 rather than keep running.
    """

    name: str
    config_hash: str
    container_ids: tuple[str, ...]
    completion: bool = False

    def __post_init__(self) -> None:
        """Reject empty names or hashes and empty, blank or duplicate IDs."""
        ids = self.container_ids
        if (
            type(self.name) is not str
            or not self.name
            or type(self.config_hash) is not str
            or not self.config_hash
            or type(self.completion) is not bool
            or type(ids) is not tuple
            or not ids
            or any(type(identity) is not str or not identity for identity in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("Compose services need a name, hash and distinct IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ComposeTarget:
    """Selected Compose services with fixed containers and configuration hashes.

    The target holds full container IDs, so keep it away from untrusted output.
    One-off ``compose run`` containers are never members. A hash records the
    service configuration, not its replica count; the container IDs fix that.
    ``max_restarts`` is a fixed restart budget for long-running containers, or
    ``None`` to leave Docker's ``RestartCount`` unchecked.
    ``fail_on_exit`` opts into failure for eligible long-running exits.
    ``fail_on_unhealthy`` opts into failure for eligible unhealthy containers.
    ``fail_on_restart_budget`` opts into failure for a long-running container
    past ``max_restarts``, and needs that budget.
    """

    client_config: ClientConfig = field(repr=False, compare=False)
    services: tuple[ComposeService, ...]
    max_restarts: int | None = None
    fail_on_exit: bool = False
    fail_on_unhealthy: bool = False
    fail_on_restart_budget: bool = False

    def __post_init__(self) -> None:
        """Reject empty membership, duplicate names or IDs, or a bad restart budget."""
        services = self.services
        if (
            type(services) is not tuple
            or not services
            or any(type(service) is not ComposeService for service in services)
        ):
            raise ValueError("Compose target must contain selected services")
        budget = self.max_restarts
        if budget is not None and (type(budget) is not int or budget < 0):
            raise ValueError("Restart budget must be None or a nonnegative integer")
        if type(self.fail_on_exit) is not bool:
            raise ValueError("fail_on_exit must be a bool")
        if type(self.fail_on_unhealthy) is not bool:
            raise ValueError("fail_on_unhealthy must be a bool")
        if type(self.fail_on_restart_budget) is not bool:
            raise ValueError("fail_on_restart_budget must be a bool")
        if self.fail_on_restart_budget and budget is None:
            raise ValueError("fail_on_restart_budget needs max_restarts")
        identities = [
            identity for service in services for identity in service.container_ids
        ]
        if len({service.name for service in services}) != len(services) or len(
            set(identities)
        ) != len(identities):
            raise ValueError("Compose target must contain distinct services and IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContainerTarget:
    """Fixed container IDs, completion designation, restart budget and failure policies."""

    client_config: ClientConfig = field(repr=False, compare=False)
    container_ids: tuple[str, ...]
    completion: tuple[str, ...] = ()
    max_restarts: int | None = None
    fail_on_exit: bool = False
    fail_on_unhealthy: bool = False
    fail_on_restart_budget: bool = False

    def __post_init__(self) -> None:
        """Reject invalid membership, completion IDs, restart budget or policies."""
        ids, completion = self.container_ids, self.completion
        if (
            type(ids) is not tuple
            or not ids
            or any(type(identity) is not str or not identity for identity in ids)
            or len(set(ids)) != len(ids)
            or type(completion) is not tuple
            or any(type(identity) is not str or not identity for identity in completion)
            or len(set(completion)) != len(completion)
            or not set(completion) <= set(ids)
        ):
            raise ValueError("Container target must contain distinct IDs")
        budget = self.max_restarts
        if budget is not None and (type(budget) is not int or budget < 0):
            raise ValueError("Restart budget must be None or a nonnegative integer")
        if type(self.fail_on_exit) is not bool:
            raise ValueError("fail_on_exit must be a bool")
        if type(self.fail_on_unhealthy) is not bool:
            raise ValueError("fail_on_unhealthy must be a bool")
        if type(self.fail_on_restart_budget) is not bool:
            raise ValueError("fail_on_restart_budget must be a bool")
        if self.fail_on_restart_budget and budget is None:
            raise ValueError("fail_on_restart_budget needs max_restarts")


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeTarget:
    """Fixed node identities and the subset required to remain reachable managers."""

    client_config: ClientConfig = field(repr=False, compare=False)
    node_ids: tuple[str, ...]
    manager_ids: tuple[str, ...] = ()
    require_leader: bool = False

    def __post_init__(self) -> None:
        """Reject invalid membership or a manager outside the selected nodes."""
        nodes, managers = self.node_ids, self.manager_ids
        if (
            type(nodes) is not tuple
            or not nodes
            or any(type(node) is not str or not node for node in nodes)
            or len(set(nodes)) != len(nodes)
            or type(managers) is not tuple
            or any(type(node) is not str or not node for node in managers)
            or len(set(managers)) != len(managers)
            or not set(managers) <= set(nodes)
        ):
            raise ValueError("Nodes and managers must be distinct selected IDs")
        if type(self.require_leader) is not bool or (
            self.require_leader and not managers
        ):
            raise ValueError("require_leader needs a bool and designated managers")


@dataclass(frozen=True, slots=True)
class Success:
    """All fixed services met the captured convergence conditions."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Failure:
    """Terminal outcome; the optional cause may contain confidential details."""

    reason: Literal[
        "unsupported",
        "malformed",
        "superseded",
        "update_failed",
        "completion_failed",
        "restart_budget_exceeded",
        "exited",
        "unhealthy",
        "observation",
    ]
    service_id: str | None = None
    state: str | None = None
    cause: Exception | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeadlineDetail:
    """Affected-service counts from the latest complete polling cycle.

    Each reason counts a service at most once; reasons can overlap. Counts describe
    a past observation over captured membership, not daemon state at expiry. No
    service identities, task or container payloads, client settings or failure
    causes are kept.

    The container counts apply to Compose and standalone container targets. Each
    standalone container counts as one captured service. A service is counted when any
    of its containers has a status other than ``running`` (so a restarting, paused
    or exited-0 container is also a non-running one), is restarting, is paused, or
    has a healthcheck whose status is not ``healthy``, including ``starting``.
    Those four apply only to long-running services. A completion service is counted
    as unfinished when any of its containers isn't ``exited`` with code 0 and not
    restarting; a nonzero exit that isn't restarting ends the wait as a failure
    instead of being counted.

    ``restart_budget_exceeded_containers`` counts long-running services with any
    container whose latest observed ``RestartCount`` exceeds the target's
    ``max_restarts``. It stays zero when the budget is disabled, and a later
    within-budget observation clears it. With ``fail_on_restart_budget`` an
    over-budget reading ends the wait as a failure, so it is never published.

    For node targets each selected node is one captured unit. The node counters
    count non-ready state, non-active availability, designated managers no longer
    holding the manager role, and designated managers without reachable status.
    A demoted node can contribute to both manager counters. When leadership is
    required, ``leader_mismatch_managers`` is zero for exactly one observed leader,
    otherwise the number of designated managers.
    """

    captured_services: int
    missing_ready_slots: int = 0
    duplicate_slots: int = 0
    stale_templates: int = 0
    non_running_tasks: int = 0
    invalid_task_shape: int = 0
    ongoing_updates: int = 0
    missing_ready_nodes: int = 0
    duplicate_node_assignments: int = 0
    missing_node_assignments: int = 0
    outside_approved_nodes: int = 0
    non_running_containers: int = 0
    restarting_containers: int = 0
    paused_containers: int = 0
    unhealthy_containers: int = 0
    unfinished_completion_containers: int = 0
    restart_budget_exceeded_containers: int = 0
    non_ready_nodes: int = 0
    unavailable_nodes: int = 0
    non_manager_nodes: int = 0
    unreachable_managers: int = 0
    leader_mismatch_managers: int = 0

    def __post_init__(self) -> None:
        """Reject non-integer counts or counts outside captured membership."""
        if type(self.captured_services) is not int or self.captured_services < 0:
            raise ValueError("Captured membership must be a nonnegative integer")
        counts = (
            self.missing_ready_slots,
            self.duplicate_slots,
            self.stale_templates,
            self.non_running_tasks,
            self.invalid_task_shape,
            self.ongoing_updates,
            self.missing_ready_nodes,
            self.duplicate_node_assignments,
            self.missing_node_assignments,
            self.outside_approved_nodes,
            self.non_running_containers,
            self.restarting_containers,
            self.paused_containers,
            self.unhealthy_containers,
            self.unfinished_completion_containers,
            self.restart_budget_exceeded_containers,
            self.non_ready_nodes,
            self.unavailable_nodes,
            self.non_manager_nodes,
            self.unreachable_managers,
            self.leader_mismatch_managers,
        )
        if any(
            type(count) is not int or not 0 <= count <= self.captured_services
            for count in counts
        ):
            raise ValueError("Affected-service counts must be bounded by membership")


@dataclass(frozen=True, slots=True)
class Deadline:
    """The total deadline expired, optionally retaining a past complete cycle.

    Detail is absent if no cycle completed before expiry, including during capture.
    Only the concrete aggregate is accepted, excluding payload-bearing subclasses.
    """

    detail: DeadlineDetail | None = None

    def __post_init__(self) -> None:
        """Reject arbitrary payloads and extended aggregate types."""
        if self.detail is not None and type(self.detail) is not DeadlineDetail:
            raise ValueError("Deadline detail must be a concrete DeadlineDetail")


@dataclass(frozen=True, slots=True)
class Cancelled:
    """An active cancellation token was set."""


type Outcome = Success | Failure | Deadline | Cancelled
