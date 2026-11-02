import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import override

import pytest

from pywhales import DockerClient
from pywhales.admission import EndpointIdentity
from pywhales.convergence import (
    Cancelled,
    ComposeTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    Outcome,
    Success,
    capture_compose,
    wait,
)
from pywhales.inventory import InspectionPolicy
from pywhales.reconciliation import (
    ActionContext,
    AmbiguousMutation,
    AttemptState,
    Controller,
    DesiredDescription,
    IdentityConflict,
    InputReference,
    InventoryDisposition,
    InventoryEvidence,
    LeaseRequest,
    MemoryJournalStore,
    OperationKey,
    OperationSpec,
    ReconciliationAdapters,
    ResourceIdentity,
)
from tests.pywhales.test_convergence import (
    A,
    B,
    capture,
    compose_target,
    container,
    service,
    snapshot,
    store_compose,
    task,
)
from tests.pywhales.test_convergence import (
    fake_cli as fake_cli,
)
from tests.pywhales.test_convergence import (
    policy as policy,
)
from tests.pywhales.test_convergence import (
    store as store_cli,
)


def spec() -> OperationSpec:
    return OperationSpec(
        OperationKey(EndpointIdentity("manager-a"), "deploy-42"),
        DesiredDescription(
            approved_input=InputReference("a" * 64),
            owner="team-a",
            resource_class="ephemeral",
            target="web",
        ),
    )


class Endpoint:
    def __init__(self) -> None:
        self.resources: tuple[ResourceIdentity, ...] = ()
        self.trace: list[str] = []
        self.lost_response = False
        self.outcome: Outcome = Success()
        self.disposition: InventoryDisposition | None = None

    def inventory(self, context: ActionContext) -> InventoryEvidence:
        context.check_current()
        self.trace.append("inventory")
        return InventoryEvidence(
            self.disposition
            or (
                InventoryDisposition.APPLIED
                if self.resources
                else InventoryDisposition.ABSENT
            ),
            self.resources,
        )

    def apply(self, context: ActionContext) -> None:
        context.check_current()
        self.trace.append("mutate")
        desired, key = context.spec.desired, context.spec.key
        self.resources = (
            ResourceIdentity(
                "service",
                "service-id",
                key.endpoint.alias,
                desired.owner,
                desired.resource_class,
                key.operation,
            ),
        )
        if self.lost_response:
            raise AmbiguousMutation()

    def observe(
        self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
    ) -> Outcome:
        context.check_current()
        self.trace.append("observe")
        assert resources == self.resources
        return self.outcome


def controller(store: MemoryJournalStore, endpoint: Endpoint) -> Controller:
    return Controller(
        store, ReconciliationAdapters(spec().key.endpoint, endpoint, endpoint, endpoint)
    )


def test_restart_same_spec_returns_existing_result_without_mutation() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    first = controller(store, endpoint)
    completed = first.reconcile(
        first.claim(first.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.trace.clear()
    restarted = controller(store, endpoint)
    existing = restarted.submit(spec())
    observed = restarted.reconcile(
        restarted.claim(existing, LeaseRequest("worker-b", 30))
    )
    assert completed.progress.state is observed.progress.state is AttemptState.SUCCEEDED
    assert observed.progress.attempts == 1
    assert endpoint.trace == ["inventory", "observe"]


def test_conflicting_intent_never_invokes_endpoint() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    owner = controller(store, endpoint)
    original = owner.submit(spec())
    conflicting = replace(
        spec(), desired=replace(spec().desired, approved_input=InputReference("b" * 64))
    )
    with pytest.raises(IdentityConflict):
        owner.submit(conflicting)
    assert store.read(spec().key) == original
    assert endpoint.trace == []


def test_remote_success_lost_response_recovers_by_inventory_without_duplicate() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.lost_response = True
    first = controller(store, endpoint)
    unknown = first.reconcile(
        first.claim(first.submit(spec()), LeaseRequest("worker-a", 30))
    )
    restarted = controller(store, endpoint)
    resolved = restarted.reconcile(
        restarted.claim(restarted.submit(spec()), LeaseRequest("worker-b", 30))
    )
    assert unknown.progress.state is AttemptState.UNKNOWN
    assert resolved.progress.state is AttemptState.SUCCEEDED
    assert endpoint.trace == ["inventory", "mutate", "inventory", "observe"]


def test_absent_after_ambiguous_attempt_never_authorizes_replay() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.lost_response = True
    owner = controller(store, endpoint)
    unknown = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.resources = ()
    endpoint.trace.clear()
    result = owner.reconcile(owner.claim(unknown, LeaseRequest("worker-b", 30)))
    assert result.progress.state is AttemptState.UNKNOWN
    assert endpoint.trace == ["inventory"]


def test_missed_event_is_repaired_by_periodic_inventory() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.outcome = Deadline()
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.outcome = Success()
    endpoint.trace.clear()
    repaired = owner.reconcile(owner.claim(pending, LeaseRequest("worker-b", 30)))
    assert pending.progress.convergence == "deadline"
    assert repaired.progress.state is AttemptState.SUCCEEDED
    assert endpoint.trace == ["inventory", "observe"]


@pytest.mark.parametrize(
    "outcome,summary,state",
    [
        (Success(), "success", AttemptState.SUCCEEDED),
        (Deadline(), "deadline", AttemptState.OBSERVING),
        (Cancelled(), "cancelled", AttemptState.OBSERVING),
        (
            Failure(
                reason="superseded",
                state="confidential-state",
                cause=RuntimeError("credential-payload"),
            ),
            "superseded",
            AttemptState.FAILED,
        ),
        (Failure(reason="observation"), "observation", AttemptState.FAILED),
        (
            Failure(
                reason="completion_failed", cause=RuntimeError("credential-payload")
            ),
            "completion_failed",
            AttemptState.FAILED,
        ),
    ],
)
def test_public_convergence_outcomes_are_projected_without_raw_fields(
    outcome: Outcome, summary: str, state: AttemptState
) -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.outcome = outcome
    owner = controller(store, endpoint)
    result = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    assert (result.progress.state, result.progress.convergence) == (state, summary)
    assert "credential-payload" not in repr(result)
    assert "confidential-state" not in repr(result)


def test_foreign_owned_resources_cannot_be_adopted() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.resources = (
        ResourceIdentity(
            "service",
            "service-id",
            "manager-a",
            "other-owner",
            "ephemeral",
            "deploy-42",
        ),
    )
    owner = controller(store, endpoint)
    with pytest.raises(IdentityConflict):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30)))
    assert endpoint.trace == ["inventory"]


def test_preexisting_applied_resources_do_not_enable_later_mutation() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.resources = (
        ResourceIdentity(
            "service", "service-id", "manager-a", "team-a", "ephemeral", "deploy-42"
        ),
    )
    owner = controller(store, endpoint)
    existing = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.resources = ()
    endpoint.disposition = InventoryDisposition.INDETERMINATE
    uncertain = owner.reconcile(owner.claim(existing, LeaseRequest("worker-b", 30)))
    endpoint.disposition = InventoryDisposition.ABSENT
    endpoint.trace.clear()
    result = owner.reconcile(owner.claim(uncertain, LeaseRequest("worker-c", 30)))
    assert result.progress.state is AttemptState.UNKNOWN
    assert endpoint.trace == ["inventory"]


@pytest.mark.parametrize(
    "outcome",
    [
        Success(),
        Cancelled(),
        Failure(reason="superseded", cause=RuntimeError("credential-payload")),
        Deadline(),
        Deadline(DeadlineDetail(2, ongoing_updates=2)),
    ],
)
def test_later_outcome_replaces_obsolete_deadline_detail_without_replay(
    outcome: Outcome,
) -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.outcome = Deadline(
        DeadlineDetail(2, missing_ready_slots=1, stale_templates=1)
    )
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    assert pending.progress.deadline_detail == endpoint.outcome.detail
    endpoint.trace.clear()
    endpoint.outcome = outcome
    result = owner.reconcile(owner.claim(pending, LeaseRequest("worker-b", 30)))
    assert result.progress.deadline_detail == (
        outcome.detail if isinstance(outcome, Deadline) else None
    )
    assert result.progress.attempts == 1
    assert endpoint.trace == ["inventory", "observe"]
    assert "credential-payload" not in repr(result)


@pytest.mark.parametrize(
    "disposition", [InventoryDisposition.ABSENT, InventoryDisposition.INDETERMINATE]
)
def test_uncertain_inventory_clears_deadline_detail_without_replay(
    disposition: InventoryDisposition,
) -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    endpoint.outcome = Deadline(DeadlineDetail(1, missing_ready_slots=1))
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.resources = ()
    endpoint.disposition = disposition
    endpoint.trace.clear()
    result = owner.reconcile(owner.claim(pending, LeaseRequest("worker-b", 30)))
    assert result.progress.state is AttemptState.UNKNOWN
    assert (
        result.progress.convergence is None and result.progress.deadline_detail is None
    )
    assert endpoint.trace == ["inventory"]


def test_real_cli_deadline_is_journaled_and_repeated_passes_only_observe(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = service("service-id")
    snapshot["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"] = [
        "PASSWORD=credential-payload"
    ]
    store_cli(tmp_path, [snapshot], [])
    target = capture(fake_cli, policy, "service-id")

    class CLIEndpoint(Endpoint):
        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            return wait(target, policy=policy, timeout=2, poll_interval=10)

    store, endpoint = MemoryJournalStore(), CLIEndpoint()
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    expected = DeadlineDetail(1, missing_ready_slots=1)
    assert pending.progress.state is AttemptState.OBSERVING
    assert pending.progress.deadline_detail == expected
    restarted = controller(store, endpoint)
    repeated = restarted.reconcile(
        restarted.claim(pending, LeaseRequest("worker-b", 30))
    )
    assert repeated.progress.deadline_detail == expected
    assert repeated.progress.attempts == 1
    rendered = json.dumps(asdict(repeated.progress))
    assert "credential-payload" not in rendered and "ContainerSpec" not in rendered
    assert "client_config" not in rendered and "cause" not in rendered
    assert asdict(expected) == json.loads(rendered)["deadline_detail"]
    current = task(service_id="service-id")
    current["Spec"] = snapshot["Spec"]["TaskTemplate"]
    store_cli(tmp_path, [snapshot], [current])
    succeeded = restarted.reconcile(
        restarted.claim(repeated, LeaseRequest("worker-c", 30))
    )
    assert succeeded.progress.state is AttemptState.SUCCEEDED
    assert succeeded.progress.deadline_detail is None
    assert endpoint.trace == [
        "inventory",
        "mutate",
        "inventory",
        "observe",
        "inventory",
        "observe",
        "inventory",
        "observe",
    ]
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert sum(call[:2] == ["service", "ps"] for call in calls) == 3


def test_real_cli_completion_failure_is_journaled_as_a_closed_reason(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(
            container(A),
            container(B, service="migrate", status="exited", exit_code=3),
        ),
    )
    target = compose_target(
        fake_cli, policy, ("web", "migrate"), completion=("migrate",)
    )

    class CLIEndpoint(Endpoint):
        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            return wait(target, policy=policy, timeout=5, poll_interval=0.01)

    store, endpoint = MemoryJournalStore(), CLIEndpoint()
    owner = controller(store, endpoint)
    result = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    assert result.progress.state is AttemptState.FAILED
    assert result.progress.convergence == "completion_failed"
    assert result.progress.deadline_detail is None
    stored = store.read(spec().key)
    assert stored is not None
    assert stored.progress.convergence == "completion_failed"
    rendered = json.dumps(asdict(result.progress))
    assert "3" not in json.loads(rendered)["convergence"]
    assert B not in rendered and "migrate" not in rendered


def test_real_cli_exited_failure_is_journaled_as_a_closed_reason(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(container(A, status="exited", exit_code=137)))
    target = capture_compose(
        fake_cli, ("web",), fail_on_exit=True, policy=policy, timeout=5
    )
    assert isinstance(target, ComposeTarget)

    class CLIEndpoint(Endpoint):
        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            return wait(target, policy=policy, timeout=5, poll_interval=1)

    store, endpoint = MemoryJournalStore(), CLIEndpoint()
    owner = controller(store, endpoint)
    result = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    assert result.progress.state is AttemptState.FAILED
    assert result.progress.convergence == "exited"
    assert result.progress.deadline_detail is None
    stored = store.read(spec().key)
    assert stored is not None and stored.progress == result.progress
    rendered = json.dumps(asdict(stored.progress))
    assert "137" not in rendered and "credential-payload" not in rendered
    assert A not in rendered and "cause" not in rendered
