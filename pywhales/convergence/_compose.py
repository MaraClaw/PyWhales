"""Capture and evaluate a Compose project's selected services each cycle.

A cycle re-lists the project's containers, inspects each one, and keeps only
counts, never container payloads. A long-running service is ready when it is
``running``; a completion service, marked at capture, when it has exited 0.
"""

import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from pydantic import ValidationError

from pywhales._observation import ObservationState, check_observation
from pywhales.client_config import ClientConfig, ClientNotFoundError, ParsingError
from pywhales.convergence._types import (
    Cancelled,
    ComposeService,
    ComposeTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    Outcome,
    Success,
)
from pywhales.convergence._wire import (
    ContainerWire,
    StandaloneContainerWire,
    inspect_containers,
    list_compose_ids,
)
from pywhales.exceptions import (
    DockerException,
    NoSuchContainer,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)

SLEEP_SLICE = 0.05

_INTERRUPTIONS = (
    ObservationCancelled,
    ObservationTimeout,
    ValidationError,
    ParsingError,
    DockerException,
    ObservationLimitError,
    OSError,
    ClientNotFoundError,
    ValueError,
)


def _outcome(
    error: Exception, last: DeadlineDetail | None
) -> Cancelled | Deadline | Failure:
    """Map an interruption to its outcome without keeping malformed input."""
    match error:
        case ObservationCancelled():
            return Cancelled()
        case ObservationTimeout():
            return Deadline(last)
        case ValidationError() | ParsingError():
            return Failure(reason="malformed")
        case _:
            return Failure(reason="observation", cause=error)


@dataclass(frozen=True, slots=True)
class Selection:
    """Caller-chosen services, completion designation and optional restart budget."""

    services: tuple[str, ...]
    completion: tuple[str, ...]
    max_restarts: int | None
    fail_on_exit: bool = False
    fail_on_unhealthy: bool = False
    fail_on_restart_budget: bool = False


def validate_selection(config: ClientConfig, selection: Selection) -> None:
    """Reject an invalid selection or a dry-run client before any launch."""
    services, completion = selection.services, selection.completion
    if (
        type(services) is not tuple
        or not services
        or any(type(name) is not str or not name for name in services)
        or len(set(services)) != len(services)
    ):
        raise ValueError("Services must be a nonempty tuple of distinct names")
    if (
        type(completion) is not tuple
        or any(type(name) is not str or not name for name in completion)
        or len(set(completion)) != len(completion)
        or not set(completion) <= set(services)
    ):
        raise ValueError("Completion services must be distinct selected services")
    budget = selection.max_restarts
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("Restart budget must be None or a nonnegative integer")
    if type(selection.fail_on_exit) is not bool:
        raise ValueError("fail_on_exit must be a bool")
    if type(selection.fail_on_unhealthy) is not bool:
        raise ValueError("fail_on_unhealthy must be a bool")
    if type(selection.fail_on_restart_budget) is not bool:
        raise ValueError("fail_on_restart_budget must be a bool")
    if selection.fail_on_restart_budget and budget is None:
        raise ValueError("fail_on_restart_budget needs max_restarts")
    if config.compose_dry_run:
        raise ValueError("A dry-run client cannot be captured")


def list_members(
    config: ClientConfig, services: tuple[str, ...]
) -> list[ContainerWire]:
    """List and inspect the selected services' containers, leaving out one-offs."""
    containers = inspect_containers(config, list_compose_ids(config, services))
    return [item for item in containers if item.config.labels.oneoff != "True"]


def _restarts(container: ContainerWire | StandaloneContainerWire) -> int:
    """Return Docker's top-level ``RestartCount``, which must be a real integer."""
    count = container.restart_count
    if type(count) is not int or count < 0:
        raise ParsingError("Invalid restart count")
    return count


def snapshot_target(
    config: ClientConfig, selection: Selection
) -> ComposeTarget | Failure:
    """Capture each selected service's current containers and single config hash."""
    services, completion = selection.services, selection.completion
    members = list_members(config, services)
    if {member.config.labels.service for member in members} - set(services):
        raise ParsingError("Container outside the selected services")
    if selection.max_restarts is not None:
        for member in members:
            if member.config.labels.service not in completion:
                _restarts(member)
    captured: list[ComposeService] = []
    for name in services:
        group = [member for member in members if member.config.labels.service == name]
        hashes = {member.config.labels.config_hash for member in group}
        if len(hashes) != 1:
            return Failure(reason="unsupported")
        config_hash = hashes.pop()
        if config_hash is None or not config_hash:
            raise ParsingError("Missing Compose config hash")
        captured.append(
            ComposeService(
                name=name,
                config_hash=config_hash,
                container_ids=tuple(member.id for member in group),
                completion=name in completion,
            )
        )
    return ComposeTarget(
        client_config=config,
        services=tuple(captured),
        max_restarts=selection.max_restarts,
        fail_on_exit=selection.fail_on_exit,
        fail_on_unhealthy=selection.fail_on_unhealthy,
        fail_on_restart_budget=selection.fail_on_restart_budget,
    )


def capture_project(
    config: ClientConfig,
    selection: Selection,
    scope: AbstractContextManager[ObservationState],
) -> ComposeTarget | Failure | Deadline | Cancelled:
    """Capture the selected services inside an already bounded observation scope."""
    try:
        with scope:
            return snapshot_target(config, selection)
    except _INTERRUPTIONS as error:
        return _outcome(error, None)


def _affected(
    groups: Iterable[list[ContainerWire | StandaloneContainerWire]],
    blocked: Callable[[ContainerWire | StandaloneContainerWire], bool],
) -> int:
    return sum(any(map(blocked, group)) for group in groups)


def _finished(container: ContainerWire | StandaloneContainerWire) -> bool:
    state = container.state
    return state.status == "exited" and state.exit_code == 0 and not state.restarting


def _failed(container: ContainerWire | StandaloneContainerWire) -> bool:
    state = container.state
    return state.status == "exited" and state.exit_code != 0 and not state.restarting


def check_compose(target: ComposeTarget) -> DeadlineDetail | Failure:
    """Re-list the project, then count each blocker once per affected service.

    Membership is the non-one-off containers of the selected services. Any added or
    vanished container, or a changed service label or config hash, is superseded.
    A completion container that exited nonzero and isn't restarting is a failure.
    """
    config = target.client_config
    if config.compose_dry_run:
        return Failure(reason="unsupported")
    try:
        members = list_members(
            config, tuple(service.name for service in target.services)
        )
    except NoSuchContainer:
        return Failure(reason="superseded")
    current = {member.id: member for member in members}
    if {
        identity: (member.config.labels.service, member.config.labels.config_hash)
        for identity, member in current.items()
    } != {
        identity: (service.name, service.config_hash)
        for service in target.services
        for identity in service.container_ids
    }:
        return Failure(reason="superseded")
    groups: list[tuple[bool, list[ContainerWire | StandaloneContainerWire]]] = [
        (service.completion, [current[identity] for identity in service.container_ids])
        for service in target.services
    ]
    return evaluate_groups(
        groups,
        target.max_restarts,
        target.fail_on_exit,
        target.fail_on_unhealthy,
        target.fail_on_restart_budget,
    )


def evaluate_groups(
    groups: list[tuple[bool, list[ContainerWire | StandaloneContainerWire]]],
    budget: int | None,
    fail_on_exit: bool,
    fail_on_unhealthy: bool,
    fail_on_restart_budget: bool,
) -> DeadlineDetail | Failure:
    """Evaluate grouped containers using the shared Compose readiness rules."""
    completion = [group for done, group in groups if done]
    if any(_failed(container) for group in completion for container in group):
        return Failure(reason="completion_failed")
    running = [group for done, group in groups if not done]
    over_budget = 0
    if budget is not None:
        counts = [[_restarts(container) for container in group] for group in running]
        over_budget = sum(any(count > budget for count in group) for group in counts)
    if fail_on_restart_budget and over_budget:
        return Failure(reason="restart_budget_exceeded")
    if fail_on_exit and any(
        container.state.status == "exited"
        and not container.state.restarting
        and (budget is None or _restarts(container) <= budget)
        for group in running
        for container in group
    ):
        return Failure(reason="exited")
    if fail_on_unhealthy and any(
        container.state.status == "running"
        and not container.state.paused
        and not container.state.restarting
        and (budget is None or _restarts(container) <= budget)
        and container.state.health is not None
        and container.state.health.status == "unhealthy"
        for group in running
        for container in group
    ):
        return Failure(reason="unhealthy")
    return DeadlineDetail(
        captured_services=len(groups),
        non_running_containers=_affected(
            running, lambda container: container.state.status != "running"
        ),
        restarting_containers=_affected(
            running, lambda container: container.state.restarting
        ),
        paused_containers=_affected(running, lambda container: container.state.paused),
        unhealthy_containers=_affected(
            running,
            lambda container: (
                (container.config.healthcheck and container.state.health is None)
                or (
                    container.state.health is not None
                    and container.state.health.status != "healthy"
                )
            ),
        ),
        unfinished_completion_containers=_affected(
            completion, lambda container: not _finished(container)
        ),
        restart_budget_exceeded_containers=over_budget,
    )


def _settled(cycle: DeadlineDetail | Failure) -> bool:
    return isinstance(cycle, DeadlineDetail) and cycle == DeadlineDetail(
        cycle.captured_services
    )


def _pause(state: ObservationState, interval: float) -> None:
    """Sleep in short slices so cancellation and expiry are noticed promptly."""
    end = time.monotonic() + interval
    if state.deadline is not None:
        end = min(end, state.deadline)
    while (now := time.monotonic()) < end:
        check_observation(state)
        time.sleep(min(end - now, SLEEP_SLICE))


def wait_compose(
    target: ComposeTarget,
    scope: AbstractContextManager[ObservationState],
    poll_interval: float,
) -> Outcome:
    """Poll the captured services under an already bounded observation scope.

    Every cycle re-lists membership. Once all containers are ready the project is
    evaluated again, and success needs that final pass to be ready and unchanged.
    The scope is entered here, so a deadline reached as it exits still yields the
    latest complete cycle's counts.
    """
    return wait_groups(lambda: check_compose(target), scope, poll_interval)


def wait_groups(
    check: Callable[[], DeadlineDetail | Failure],
    scope: AbstractContextManager[ObservationState],
    poll_interval: float,
) -> Outcome:
    """Run complete cycles and a final all-clear under the shared clock."""
    last_complete: DeadlineDetail | None = None
    try:
        with scope as state:
            while True:
                check_observation(state)
                cycle = check()
                if _settled(cycle):
                    cycle = check()
                if isinstance(cycle, Failure):
                    return cycle
                check_observation(state)
                last_complete = cycle
                if _settled(cycle):
                    return Success()
                _pause(state, poll_interval)
    except _INTERRUPTIONS as error:
        return _outcome(error, last_complete)
