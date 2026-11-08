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
    NodeTarget,
    Outcome,
    ServiceTarget,
    capture_compose,
    capture_containers,
    capture_nodes,
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
        client,
        ("web",),
        max_restarts=3,
        fail_on_exit=True,
        fail_on_unhealthy=True,
        fail_on_restart_budget=True,
        policy=policy,
        timeout=1.0,
    )
    assert_type(budgeted, ComposeTarget | Failure | Deadline | Cancelled)
    if isinstance(budgeted, ComposeTarget):
        assert_type(budgeted.max_restarts, int | None)
        assert_type(budgeted.fail_on_exit, bool)
        assert_type(budgeted.fail_on_unhealthy, bool)
        assert_type(budgeted.fail_on_restart_budget, bool)
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
                "restart_budget_exceeded",
                "exited",
                "unhealthy",
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
        fail_on_unhealthy=True,
        fail_on_restart_budget=True,
        policy=policy,
        timeout=1.0,
    )
    assert_type(captured, ContainerTarget | Failure | Deadline | Cancelled)
    if isinstance(captured, ContainerTarget):
        assert_type(captured.container_ids, tuple[str, ...])
        assert_type(captured.completion, tuple[str, ...])
        assert_type(captured.max_restarts, int | None)
        assert_type(captured.fail_on_exit, bool)
        assert_type(captured.fail_on_unhealthy, bool)
        assert_type(captured.fail_on_restart_budget, bool)
        assert_type(
            wait(captured, policy=policy, timeout=1.0, poll_interval=0.1), Outcome
        )


def _check_node_contract(client: DockerClient, policy: InspectionPolicy) -> None:
    captured = capture_nodes(
        client,
        ("node-a", "node-b"),
        manager_ids=("node-a",),
        require_leader=True,
        policy=policy,
        timeout=1,
    )
    assert_type(captured, NodeTarget | Failure | Deadline | Cancelled)
    if isinstance(captured, NodeTarget):
        assert_type(captured.node_ids, tuple[str, ...])
        assert_type(captured.manager_ids, tuple[str, ...])
        assert_type(captured.require_leader, bool)
        assert_type(
            wait(captured, policy=policy, timeout=1, poll_interval=0.1), Outcome
        )
    assert_type(DeadlineDetail(1).non_ready_nodes, int)
    assert_type(DeadlineDetail(1).unavailable_nodes, int)
    assert_type(DeadlineDetail(1).non_manager_nodes, int)
    assert_type(DeadlineDetail(1).unreachable_managers, int)
    assert_type(DeadlineDetail(1).leader_mismatch_managers, int)
