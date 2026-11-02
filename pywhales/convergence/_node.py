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
from pywhales.convergence._wire import NodeMissing, inspect_node


def capture_selected(
    target: NodeTarget, scope: AbstractContextManager[ObservationState]
) -> NodeTarget | Failure | Deadline | Cancelled:
    """Validate all selected identities within the capture scope."""
    try:
        with scope:
            for identity in target.node_ids:
                check_observation()
                inspect_node(target.client_config, identity)
            return target
    except NodeMissing as error:
        return Failure(reason="observation", cause=error.cause)
    except _INTERRUPTIONS as error:
        return _outcome(error, None)


def check_nodes(target: NodeTarget) -> DeadlineDetail | Failure:
    """Inspect fixed identities and count each readiness blocker per node."""
    if target.client_config.compose_dry_run:
        return Failure(reason="unsupported")
    non_ready = unavailable = non_manager = unreachable = 0
    for identity in target.node_ids:
        check_observation()
        try:
            node = inspect_node(target.client_config, identity)
        except NodeMissing:
            return Failure(reason="superseded")
        non_ready += node.status.state != "ready"
        unavailable += node.spec.availability != "active"
        if identity in target.manager_ids:
            non_manager += node.spec.role != "manager"
            unreachable += (
                node.manager_status is None
                or node.manager_status.reachability != "reachable"
            )
    return DeadlineDetail(
        len(target.node_ids),
        non_ready_nodes=non_ready,
        unavailable_nodes=unavailable,
        non_manager_nodes=non_manager,
        unreachable_managers=unreachable,
    )


def wait_nodes(
    target: NodeTarget,
    scope: AbstractContextManager[ObservationState],
    poll_interval: float,
) -> Outcome:
    """Require two complete clear passes under the bounded observation scope."""
    return wait_groups(lambda: check_nodes(target), scope, poll_interval)
