"""Capture and evaluate fixed standalone container membership."""

from contextlib import AbstractContextManager

from pywhales._observation import ObservationState, check_observation
from pywhales.client_config import ClientConfig
from pywhales.convergence._compose import (
    _INTERRUPTIONS,
    Selection,
    _outcome,
    _restarts,
    evaluate_groups,
    wait_groups,
)
from pywhales.convergence._types import (
    Cancelled,
    ContainerTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    Outcome,
)
from pywhales.convergence._wire import (
    ContainerWire,
    StandaloneContainerWire,
    inspect_standalone,
    resolve_container,
)
from pywhales.exceptions import NoSuchContainer


def capture_selected(
    config: ClientConfig,
    selection: Selection,
    scope: AbstractContextManager[ObservationState],
) -> ContainerTarget | Failure | Deadline | Cancelled:
    """Resolve selections within the bounded scope, retaining only full IDs."""
    try:
        with scope:
            ids: list[str] = []
            completed: list[str] = []
            for reference in selection.services:
                check_observation()
                member = resolve_container(config, reference)
                if member.id in ids:
                    return Failure(reason="malformed")
                ids.append(member.id)
                if reference in selection.completion:
                    completed.append(member.id)
                elif selection.max_restarts is not None:
                    _restarts(member)
            return ContainerTarget(
                client_config=config,
                container_ids=tuple(ids),
                completion=tuple(completed),
                max_restarts=selection.max_restarts,
                fail_on_exit=selection.fail_on_exit,
                fail_on_unhealthy=selection.fail_on_unhealthy,
            )
    except _INTERRUPTIONS as error:
        return _outcome(error, None)


def check_containers(target: ContainerTarget) -> DeadlineDetail | Failure:
    """Inspect captured IDs and evaluate each as a separate readiness unit."""
    if target.client_config.compose_dry_run:
        return Failure(reason="unsupported")
    groups: list[tuple[bool, list[ContainerWire | StandaloneContainerWire]]] = []
    for identity in target.container_ids:
        check_observation()
        try:
            member = inspect_standalone(target.client_config, identity)
        except NoSuchContainer:
            return Failure(reason="superseded")
        if member.id != identity:
            return Failure(reason="superseded")
        groups.append((identity in target.completion, [member]))
    return evaluate_groups(
        groups, target.max_restarts, target.fail_on_exit, target.fail_on_unhealthy
    )


def wait_containers(
    target: ContainerTarget,
    scope: AbstractContextManager[ObservationState],
    poll_interval: float,
) -> Outcome:
    """Wait using Compose's cycle, final recheck, and interruption handling."""
    return wait_groups(lambda: check_containers(target), scope, poll_interval)
