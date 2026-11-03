"""Capture and evaluate fixed Swarm node membership without mutation."""

from contextlib import AbstractContextManager

from pywhales._observation import ObservationState, check_observation
from pywhales.convergence._compose import _INTERRUPTIONS, _outcome, wait_groups
from pywhales.convergence._types import (
    Cancelled,
    Deadline,
    DeadlineDetail,
    Failure,
    NodeTarget,
    Outcome,
)
from pywhales.convergence._wire import NodeMissing, NodeWire, inspect_node


def _leader_valid(target: NodeTarget, node: NodeWire) -> bool:
    return (
        not target.require_leader
        or node.id not in target.manager_ids
        or node.spec.role != "manager"
        or node.manager_status is None
        or type(node.manager_status.leader) is bool
    )


def capture_selected(
    target: NodeTarget, scope: AbstractContextManager[ObservationState]
) -> NodeTarget | Failure | Deadline | Cancelled:
    """Validate all selected identities within the capture scope."""
    try:
        with scope:
            for identity in target.node_ids:
                check_observation()
                node = inspect_node(target.client_config, identity)
                if not _leader_valid(target, node):
                    return Failure(reason="malformed")
            return target
    except NodeMissing as error:
        return Failure(reason="observation", cause=error.cause)
    except _INTERRUPTIONS as error:
        return _outcome(error, None)


def check_nodes(target: NodeTarget) -> DeadlineDetail | Failure:
    """Inspect fixed identities and count each readiness blocker per node."""
    if target.client_config.compose_dry_run:
        return Failure(reason="unsupported")
    non_ready = unavailable = non_manager = unreachable = leaders = 0
    for identity in target.node_ids:
        check_observation()
        try:
            node = inspect_node(target.client_config, identity)
        except NodeMissing:
            return Failure(reason="superseded")
        if not _leader_valid(target, node):
            return Failure(reason="malformed")
        non_ready += node.status.state != "ready"
        unavailable += node.spec.availability != "active"
        if identity in target.manager_ids:
            non_manager += node.spec.role != "manager"
            unreachable += (
                node.manager_status is None
                or node.manager_status.reachability != "reachable"
            )
            leaders += (
                node.spec.role == "manager"
                and node.manager_status is not None
                and node.manager_status.leader is True
            )
    return DeadlineDetail(
        len(target.node_ids),
        non_ready_nodes=non_ready,
        unavailable_nodes=unavailable,
        non_manager_nodes=non_manager,
        unreachable_managers=unreachable,
        leader_mismatch_managers=(
            len(target.manager_ids) if target.require_leader and leaders != 1 else 0
        ),
    )


def wait_nodes(
    target: NodeTarget,
    scope: AbstractContextManager[ObservationState],
    poll_interval: float,
) -> Outcome:
    """Require two complete clear passes under the bounded observation scope."""
    return wait_groups(lambda: check_nodes(target), scope, poll_interval)
