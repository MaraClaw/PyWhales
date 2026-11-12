"""Evaluate one captured service without retaining inspection payloads."""

from collections.abc import Sequence
from dataclasses import dataclass

from pywhales.client_config import ParsingError
from pywhales.convergence._types import DeadlineDetail, Failure, ServiceTarget
from pywhales.convergence._wire import (
    ServiceWire,
    canonical,
    inspect_service,
    inspect_tasks,
    same_generation,
)
from pywhales.enums import TaskState
from pywhales.utils import run


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    """Independent blocker flags for a single service in a provisional cycle."""

    missing_ready_slots: bool
    duplicate_slots: bool
    stale_templates: bool
    non_running_tasks: bool
    invalid_task_shape: bool
    ongoing_updates: bool
    missing_ready_nodes: bool = False
    duplicate_node_assignments: bool = False
    missing_node_assignments: bool = False
    outside_approved_nodes: bool = False

    @property
    def ready(self) -> bool:
        """Return whether every observed convergence condition was satisfied."""
        return not any(
            (
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
            )
        )


def summarize(
    observations: Sequence[ServiceObservation], rechecked_updates: int
) -> DeadlineDetail:
    """Count affected services over one complete cycle of captured membership."""
    return DeadlineDetail(
        captured_services=len(observations),
        missing_ready_slots=sum(item.missing_ready_slots for item in observations),
        duplicate_slots=sum(item.duplicate_slots for item in observations),
        stale_templates=sum(item.stale_templates for item in observations),
        non_running_tasks=sum(item.non_running_tasks for item in observations),
        invalid_task_shape=sum(item.invalid_task_shape for item in observations),
        ongoing_updates=sum(item.ongoing_updates for item in observations)
        + rechecked_updates,
        missing_ready_nodes=sum(item.missing_ready_nodes for item in observations),
        duplicate_node_assignments=sum(
            item.duplicate_node_assignments for item in observations
        ),
        missing_node_assignments=sum(
            item.missing_node_assignments for item in observations
        ),
        outside_approved_nodes=sum(
            item.outside_approved_nodes for item in observations
        ),
    )


def check_service(
    target: ServiceTarget, snapshot: ServiceWire | None = None
) -> ServiceObservation | Failure:
    """Check a captured generation, counting blockers only on desired-running tasks.

    Args:
        target: The captured service generation.
        snapshot: An inspection of ``target.service_id`` the caller already fetched.
            Omit it to inspect the service here. The task listing is launched here
            either way.

    Returns:
        The provisional blocker flags, or a terminal failure for the generation.
    """
    service = (
        inspect_service(target.client_config, target.service_id)
        if snapshot is None
        else snapshot
    )
    if not same_generation(target, service):
        return Failure(reason="superseded", service_id=target.service_id)
    state = service.update.state if service.update is not None else None
    is_job = target.mode in {"replicated_job", "global_job"}
    if is_job:
        state = None
    if state in {
        "paused",
        "rollback_started",
        "rollback_paused",
        "rollback_completed",
        "failed",
    }:
        return Failure(
            reason="update_failed", service_id=target.service_id, state=state
        )
    if state not in {None, "updating", "completed"}:
        return Failure(reason="malformed", service_id=target.service_id)
    ids = run(
        [
            *target.client_config.docker_cmd,
            "service",
            "ps",
            "--quiet",
            "--no-trunc",
            target.service_id,
        ]
    ).splitlines()
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        raise ParsingError("Invalid task listing")
    slots: set[int] = set()
    seen_slots: set[int] = set()
    approved = set(target.approved_node_ids or ())
    ready_nodes: set[str] = set()
    seen_nodes: set[str] = set()
    duplicate_nodes = missing_assignment = outside_nodes = False
    duplicate = stale = non_running = invalid = False
    for task in inspect_tasks(target.client_config, ids):
        if task.service_id != target.service_id:
            raise ParsingError("Task service identity mismatch")
        if is_job:
            if (
                task.job_iteration is None
                or task.job_iteration.index != target.job_iteration
            ):
                continue
            if (
                task.status.state in {"failed", "rejected"}
                and target.restart_condition == "none"
            ):
                return Failure(reason="completion_failed", service_id=target.service_id)
            if task.status.state == "complete":
                if (
                    target.mode == "replicated_job"
                    and task.slot is not None
                    and 0 <= task.slot < target.total_completions
                ):
                    slots.add(task.slot)
                if target.mode == "global_job" and task.node_id in approved:
                    ready_nodes.add(task.node_id)
            continue
        if task.desired not in TaskState or task.status.state not in TaskState:
            raise ParsingError("Unknown task state")
        if task.desired != "running":
            continue
        non_running |= task.status.state != "running"
        if target.mode == "replicated" and task.slot is not None:
            duplicate |= task.slot in seen_slots
            seen_slots.add(task.slot)
        matching = (
            task.spec is not None
            and canonical(task.spec, template=True) == target.template
        )
        stale |= task.spec is not None and not matching
        if target.mode == "global":
            missing_assignment |= not task.node_id
            if task.node_id:
                duplicate_nodes |= task.node_id in seen_nodes
                seen_nodes.add(task.node_id)
                outside_nodes |= task.node_id not in approved
            if task.spec is None:
                invalid = True
            if task.node_id in approved and task.status.state == "running" and matching:
                ready_nodes.add(task.node_id)
            continue
        if (
            task.slot is None
            or task.slot < 1
            or task.slot > target.replicas
            or task.spec is None
        ):
            invalid = True
            continue
        if task.status.state == "running" and matching:
            slots.add(task.slot)
    return ServiceObservation(
        missing_ready_slots=(
            target.mode == "replicated" and len(slots) < target.replicas
        )
        or (target.mode == "replicated_job" and len(slots) < target.total_completions),
        duplicate_slots=duplicate,
        stale_templates=stale,
        non_running_tasks=non_running,
        invalid_task_shape=invalid,
        ongoing_updates=state == "updating",
        missing_ready_nodes=target.mode in {"global", "global_job"}
        and ready_nodes != approved,
        duplicate_node_assignments=duplicate_nodes,
        missing_node_assignments=missing_assignment,
        outside_approved_nodes=outside_nodes,
    )
