"""Static Compose convergence contracts, checked by ty and not collected by pytest."""

from threading import Event
from typing import Literal, assert_type

from pywhales import DockerClient
from pywhales.convergence import (
    Cancelled,
    ComposeService,
    ComposeTarget,
    ContainerTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    Outcome,
    ServiceTarget,
    capture_compose,
    capture_containers,
    capture_service,
    wait,
)
from pywhales.inventory import InspectionPolicy


def _check_compose_contract(
    client: DockerClient, policy: InspectionPolicy, cancel: Event
) -> None:
    captured = capture_compose(
        client, ("web", "db"), policy=policy, timeout=1.0, cancel=cancel
    )
    assert_type(captured, ComposeTarget | Failure | Deadline | Cancelled)
    budgeted = capture_compose(
        client, ("web",), max_restarts=3, fail_on_exit=True, policy=policy, timeout=1.0
    )
    assert_type(budgeted, ComposeTarget | Failure | Deadline | Cancelled)
    if isinstance(budgeted, ComposeTarget):
        assert_type(budgeted.max_restarts, int | None)
        assert_type(budgeted.fail_on_exit, bool)
    assert_type(DeadlineDetail(1).restart_budget_exceeded_containers, int)
    if isinstance(captured, ComposeTarget):
        assert_type(captured.services, tuple[ComposeService, ...])
        assert_type(captured.services[0].container_ids, tuple[str, ...])
        assert_type(
            wait(captured, policy=policy, timeout=1.0, poll_interval=0.1), Outcome
        )


def _check_completion_contract(
    client: DockerClient, policy: InspectionPolicy, cancel: Event
) -> None:
    captured = capture_compose(
        client,
        ("web", "migrate"),
        completion_services=("migrate",),
        policy=policy,
        timeout=1.0,
        cancel=cancel,
    )
    assert_type(captured, ComposeTarget | Failure | Deadline | Cancelled)
    if isinstance(captured, ComposeTarget):
        assert_type(captured.services[0].completion, bool)
    if isinstance(captured, Failure):
        assert_type(
            captured.reason,
            Literal[
                "unsupported",
                "malformed",
                "superseded",
                "update_failed",
                "completion_failed",
                "exited",
                "observation",
            ],
        )
    assert_type(DeadlineDetail(1).unfinished_completion_containers, int)


def _check_job_contract(client: DockerClient, policy: InspectionPolicy) -> None:
    captured = capture_service(client, "service", policy=policy, timeout=1.0)
    if isinstance(captured, ServiceTarget):
        assert_type(
            captured.mode,
            Literal["replicated", "global", "replicated_job", "global_job"],
        )
        assert_type(captured.job_iteration, int | None)
        assert_type(captured.total_completions, int)
        assert_type(captured.restart_condition, str | None)


def _check_container_contract(client: DockerClient, policy: InspectionPolicy) -> None:
    captured = capture_containers(
        client,
        ("db", "migrate"),
        completion=("migrate",),
        max_restarts=1,
        fail_on_exit=True,
        policy=policy,
        timeout=1.0,
    )
    assert_type(captured, ContainerTarget | Failure | Deadline | Cancelled)
    if isinstance(captured, ContainerTarget):
        assert_type(captured.container_ids, tuple[str, ...])
        assert_type(captured.completion, tuple[str, ...])
        assert_type(captured.max_restarts, int | None)
        assert_type(captured.fail_on_exit, bool)
        assert_type(
            wait(captured, policy=policy, timeout=1.0, poll_interval=0.1), Outcome
        )
