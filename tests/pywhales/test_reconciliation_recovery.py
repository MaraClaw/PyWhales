import traceback
from collections.abc import Callable
from dataclasses import replace
from threading import Event, Thread
from time import monotonic
from typing import override
from unittest.mock import patch

import pytest

from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.convergence import Deadline, DeadlineDetail, Outcome
from pywhales.reconciliation import (
    ActionContext,
    AdapterUnavailable,
    AdmittedReconciliation,
    AttemptState,
    Controller,
    IdentityConflict,
    InventoryEvidence,
    JournalEntry,
    LeaseRequest,
    MemoryJournalStore,
    ResourceIdentity,
    StoreConflict,
    StoreUnavailable,
    Transition,
)
from tests.pywhales.test_admission_calls import _spawn
from tests.pywhales.test_reconciliation import Clock, Endpoint, controller, spec


class FaultStore(MemoryJournalStore):
    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        super().__init__(clock)
        self.outage = False
        self.fail_write = False
        self.commit_then_fail = False
        self.fail_renew = False
        self.renew_then_fail = False

    @override
    def verify(self, expected: JournalEntry) -> None:
        if self.outage:
            raise StoreUnavailable()
        return super().verify(expected)

    @override
    def renew(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        if self.fail_renew:
            raise StoreUnavailable()
        renewed = super().renew(expected, request)
        if self.renew_then_fail:
            raise StoreUnavailable()
        return renewed

    @override
    def compare_and_swap(
        self, expected: JournalEntry, change: Transition
    ) -> JournalEntry:
        if self.fail_write:
            raise StoreUnavailable()
        committed = super().compare_and_swap(expected, change)
        if self.commit_then_fail:
            raise StoreUnavailable()
        return committed


def test_store_unavailable_before_observation_halts_all_adapters() -> None:
    store, endpoint = FaultStore(), Endpoint()
    owner = controller(store, endpoint)
    claimed = owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    store.outage = True
    with pytest.raises(StoreUnavailable):
        owner.reconcile(claimed)
    assert endpoint.trace == []


@pytest.mark.parametrize("commit_then_fail", [False, True])
def test_failed_prelaunch_unknown_write_never_launches(commit_then_fail: bool) -> None:
    store, endpoint = FaultStore(), Endpoint()
    owner = controller(store, endpoint)
    claimed = owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    store.fail_write, store.commit_then_fail = not commit_then_fail, commit_then_fail
    with pytest.raises(StoreUnavailable):
        owner.reconcile(claimed)
    assert endpoint.trace == ["inventory"]
    current = store.read(spec().key)
    assert current is not None
    assert current.progress.attempts == int(commit_then_fail)


def test_outage_after_launch_leaves_unknown_and_recovery_inventories() -> None:
    clock = Clock()
    store = FaultStore(clock)

    class OutageEndpoint(Endpoint):
        @override
        def apply(self, context: ActionContext) -> None:
            super().apply(context)
            store.outage = True

    endpoint = OutageEndpoint()
    first = controller(store, endpoint)
    with pytest.raises(StoreUnavailable):
        first.reconcile(first.claim(first.submit(spec()), LeaseRequest("worker-a", 30)))
    current = store.read(spec().key)
    assert current is not None
    assert current.progress.state is AttemptState.UNKNOWN
    store.outage = False
    clock.now += 30
    restarted = controller(store, endpoint)
    result = restarted.reconcile(restarted.claim(current, LeaseRequest("worker-b", 30)))
    assert result.progress.state is AttemptState.SUCCEEDED
    assert endpoint.trace == ["inventory", "mutate", "inventory", "observe"]


def test_expired_worker_cannot_launch_after_inventory() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)

    class ExpiringEndpoint(Endpoint):
        @override
        def inventory(self, context: ActionContext) -> InventoryEvidence:
            evidence = super().inventory(context)
            clock.now += 10
            return evidence

    endpoint = ExpiringEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(StoreConflict):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 5)))
    assert endpoint.trace == ["inventory"]


def test_superseded_worker_cannot_launch_additional_adapter_action() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)

    class MultiActionEndpoint(Endpoint):
        @override
        def apply(self, context: ActionContext) -> None:
            super().apply(context)
            clock.now += 10
            current = store.read(context.spec.key)
            assert current is not None
            store.claim(current, LeaseRequest("worker-b", 5))
            context.check_current()
            self.trace.append("second-mutation")

    endpoint = MultiActionEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(StoreConflict):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 5)))
    assert endpoint.trace == ["inventory", "mutate"]


def test_queued_reconciliation_checks_lease_after_admission() -> None:
    clock = Clock()
    store, endpoint = MemoryJournalStore(clock), Endpoint()
    pressure = PressureClass("reconcile")
    admission = AdmissionOwner(spec().key.endpoint, {pressure: ClassLimit(1, 1)})
    plain = controller(store, endpoint)
    owner = Controller(
        store, plain.adapters, admission=AdmittedReconciliation(admission, pressure)
    )
    claimed = owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 5))
    active, release, queued = Event(), Event(), Event()

    def hold() -> None:
        active.set()
        assert release.wait(3)

    first, first_result = _spawn(lambda: admission.request(pressure).call(hold))
    wait = admission._condition.wait

    def signal(timeout: float | None = None) -> bool:
        queued.set()
        return wait(timeout)

    workers: list[Thread] = []
    try:
        assert active.wait(3)
        with patch.object(admission._condition, "wait", side_effect=signal):
            worker, result = _spawn(lambda: owner.reconcile(claimed))
            workers.append(worker)
            assert queued.wait(3)
            clock.now += 10
            release.set()
            worker.join(3)
        assert isinstance(result.get(timeout=3), StoreConflict)
        assert endpoint.trace == []
    finally:
        release.set()
        first.join(3)
        for worker in workers:
            worker.join(3)
    assert not first.is_alive()
    assert workers and all(not worker.is_alive() for worker in workers)
    assert first_result.get(timeout=3) is None
    assert admission.snapshot().classes[0].active == 0


def test_observation_error_has_no_confidential_cause_in_rendered_error() -> None:
    class FailingEndpoint(Endpoint):
        @override
        def inventory(self, context: ActionContext) -> InventoryEvidence:
            raise OSError("credential-payload")

    store, endpoint = MemoryJournalStore(), FailingEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(AdapterUnavailable) as raised:
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30)))
    rendered = "".join(traceback.format_exception(raised.value))
    assert "credential-payload" not in rendered
    assert "credential-payload" not in repr(raised.value)


def test_endpoint_mismatch_fails_before_claim_or_admission() -> None:
    store, endpoint = MemoryJournalStore(), Endpoint()
    owner = controller(store, endpoint)
    other = replace(
        spec(), key=replace(spec().key, endpoint=EndpointIdentity("manager-b"))
    )
    with pytest.raises(IdentityConflict):
        owner.submit(other)
    assert endpoint.trace == []


def test_superseded_generation_cannot_publish_old_deadline_detail() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)
    newer = DeadlineDetail(2, ongoing_updates=2)

    class SupersedingEndpoint(Endpoint):
        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            clock.now += 10
            current = store.read(context.spec.key)
            assert current is not None
            claimed = store.claim(current, LeaseRequest("worker-b", 5))
            store.compare_and_swap(
                claimed,
                Transition(
                    replace(
                        claimed.progress, convergence="deadline", deadline_detail=newer
                    ),
                    release=True,
                ),
            )
            return Deadline(DeadlineDetail(1, missing_ready_slots=1))

    endpoint = SupersedingEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(StoreConflict):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 5)))
    current = store.read(spec().key)
    assert current is not None
    assert current.generation == 2 and current.progress.deadline_detail == newer
    assert endpoint.trace == ["inventory", "mutate", "inventory", "observe"]


@pytest.mark.parametrize("commit_then_fail", [False, True])
def test_deadline_write_outage_stops_pass_and_recovers_without_replay(
    commit_then_fail: bool,
) -> None:
    clock = Clock()
    store = FaultStore(clock)
    detail = DeadlineDetail(1, missing_ready_slots=1)

    class OutageEndpoint(Endpoint):
        fail = False

        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            current = store.read(context.spec.key)
            assert current is not None and current.progress.deadline_detail is None
            if self.fail:
                store.fail_write = not commit_then_fail
                store.commit_then_fail = commit_then_fail
            return self.outcome

    endpoint = OutageEndpoint()
    endpoint.outcome = Deadline(detail)
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    endpoint.fail = True
    endpoint.trace.clear()
    with pytest.raises(StoreUnavailable):
        owner.reconcile(owner.claim(pending, LeaseRequest("worker-b", 30)))
    current = store.read(spec().key)
    assert current is not None
    assert current.progress.deadline_detail == (detail if commit_then_fail else None)
    assert endpoint.trace == ["inventory", "observe"]
    endpoint.fail = store.fail_write = store.commit_then_fail = False
    clock.now += 30
    endpoint.trace.clear()
    recovered = owner.reconcile(owner.claim(current, LeaseRequest("worker-c", 30)))
    assert recovered.progress.deadline_detail == detail
    assert recovered.progress.attempts == 1
    assert endpoint.trace == ["inventory", "observe"]


@pytest.mark.parametrize("fault", [StoreConflict, StoreUnavailable])
def test_failed_clearing_write_stops_before_further_observation(
    fault: type[Exception],
) -> None:
    class ClearingStore(MemoryJournalStore):
        fail = False

        @override
        def compare_and_swap(
            self, expected: JournalEntry, change: Transition
        ) -> JournalEntry:
            if self.fail:
                raise fault()
            return super().compare_and_swap(expected, change)

    store, endpoint = ClearingStore(), Endpoint()
    endpoint.outcome = Deadline(DeadlineDetail(1, ongoing_updates=1))
    owner = controller(store, endpoint)
    pending = owner.reconcile(
        owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30))
    )
    claimed = owner.claim(pending, LeaseRequest("worker-b", 30))
    store.fail = True
    endpoint.trace.clear()
    with pytest.raises(fault):
        owner.reconcile(claimed)
    assert endpoint.trace == ["inventory"]
    assert store.read(spec().key) == claimed


@pytest.mark.parametrize("swallowed", [False, True])
@pytest.mark.parametrize("commit_then_fail", [False, True])
def test_renewal_outage_halts_without_outcome_commit_and_recovers_without_replay(
    commit_then_fail: bool, swallowed: bool
) -> None:
    clock = Clock()
    store = FaultStore(clock)

    class OutageEndpoint(Endpoint):
        fail = True
        seen: JournalEntry | None = None

        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            if self.fail:
                self.seen = store.read(context.spec.key)
                store.fail_renew = not commit_then_fail
                store.renew_then_fail = commit_then_fail
                try:
                    context.renew_lease(30)
                except StoreUnavailable:
                    if not swallowed:
                        raise
            context.check_current()
            self.trace.append("further-action")
            return self.outcome

    endpoint = OutageEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(StoreUnavailable):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 30)))
    current, seen = store.read(spec().key), endpoint.seen
    assert current is not None and seen is not None
    assert endpoint.trace == ["inventory", "mutate", "inventory", "observe"]
    assert current.progress == seen.progress
    assert current.progress.state is AttemptState.OBSERVING
    assert current.lease is not None
    assert current.version == seen.version + int(commit_then_fail)
    endpoint.fail = store.fail_renew = store.renew_then_fail = False
    clock.now += 30
    endpoint.trace.clear()
    recovered = owner.reconcile(owner.claim(current, LeaseRequest("worker-b", 30)))
    assert recovered.progress.state is AttemptState.SUCCEEDED
    assert recovered.progress.attempts == 1
    assert endpoint.trace == ["inventory", "observe", "further-action"]


@pytest.mark.parametrize("late_renewal", [False, True])
def test_observer_that_stops_renewing_loses_its_lease_to_a_later_claimer(
    late_renewal: bool,
) -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)

    class WedgedEndpoint(Endpoint):
        @override
        def observe(
            self, context: ActionContext, resources: tuple[ResourceIdentity, ...]
        ) -> Outcome:
            super().observe(context, resources)
            clock.now += 3
            context.renew_lease(5)
            clock.now += 6
            current = store.read(context.spec.key)
            assert current is not None
            store.claim(current, LeaseRequest("worker-b", 5))
            if late_renewal:
                context.renew_lease(5)
            return self.outcome

    endpoint = WedgedEndpoint()
    owner = controller(store, endpoint)
    with pytest.raises(StoreConflict):
        owner.reconcile(owner.claim(owner.submit(spec()), LeaseRequest("worker-a", 5)))
    current = store.read(spec().key)
    assert current is not None and current.lease is not None
    assert current.generation == 2 and current.lease.worker == "worker-b"
    assert current.progress.state is AttemptState.OBSERVING
    assert current.progress.convergence is None
    assert endpoint.trace == ["inventory", "mutate", "inventory", "observe"]
