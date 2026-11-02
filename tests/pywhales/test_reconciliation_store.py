from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace

import pytest

from pywhales.admission import EndpointIdentity
from pywhales.convergence import DeadlineDetail
from pywhales.reconciliation import (
    AttemptState,
    ConvergenceSummary,
    IdentityConflict,
    InputReference,
    InvalidDescription,
    JournalEntry,
    LeaseRequest,
    MemoryJournalStore,
    Progress,
    SecretReference,
    StoreConflict,
    Transition,
    ownership_labels,
)
from tests.pywhales.test_reconciliation import spec


@dataclass(slots=True)
class Clock:
    now: float = 10

    def __call__(self) -> float:
        return self.now


def test_atomic_create_if_absent_returns_one_entry_across_workers() -> None:
    store = MemoryJournalStore()
    with ThreadPoolExecutor(max_workers=8) as workers:
        entries = list(workers.map(lambda _: store.create_if_absent(spec()), range(32)))
    assert entries == [entries[0]] * 32
    assert entries[0].version == entries[0].generation == 0
    assert store.semantics.durability == "none"


def test_operation_identity_cannot_be_rebound_to_another_endpoint() -> None:
    store = MemoryJournalStore()
    store.create_if_absent(spec())
    other = replace(
        spec(), key=replace(spec().key, endpoint=EndpointIdentity("manager-b"))
    )
    with pytest.raises(IdentityConflict):
        store.create_if_absent(other)
    with pytest.raises(IdentityConflict):
        store.read(other.key)


def test_lease_transfer_increments_generation_and_rejects_stale_writes() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)
    stale = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 5))
    clock.now = 15
    current = store.claim(stale, LeaseRequest("worker-b", 5))
    with pytest.raises(StoreConflict):
        store.compare_and_swap(stale, Transition(Progress(AttemptState.UNKNOWN, 1)))
    with pytest.raises(StoreConflict):
        store.verify(stale)
    assert (current.version, current.generation) == (2, 2)


def test_cas_checks_version_even_with_same_generation_and_worker() -> None:
    store = MemoryJournalStore()
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 30))
    updated = store.compare_and_swap(
        claimed, Transition(Progress(AttemptState.UNKNOWN, 1))
    )
    with pytest.raises(StoreConflict):
        store.compare_and_swap(claimed, Transition(Progress()))
    assert updated.version == claimed.version + 1
    assert updated.generation == claimed.generation


def test_cas_cannot_erase_attempt_history_to_authorize_duplicate() -> None:
    store = MemoryJournalStore()
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 30))
    attempted = store.compare_and_swap(
        claimed, Transition(Progress(AttemptState.UNKNOWN, 1))
    )
    with pytest.raises(StoreConflict):
        store.compare_and_swap(attempted, Transition(Progress()))


def test_live_lease_cannot_be_claimed_even_by_same_worker() -> None:
    store = MemoryJournalStore()
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 30))
    with pytest.raises(StoreConflict):
        store.claim(claimed, LeaseRequest("worker-a", 30))


def test_expired_lease_cannot_commit_without_transfer() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 5))
    clock.now = 15
    with pytest.raises(StoreConflict):
        store.compare_and_swap(claimed, Transition(Progress()))


def test_release_invalidates_worker_and_preserves_generation() -> None:
    store = MemoryJournalStore()
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 30))
    released = store.compare_and_swap(claimed, Transition(claimed.progress, True))
    with pytest.raises(StoreConflict):
        store.verify(released)
    assert released.lease is None
    assert (released.version, released.generation) == (2, 1)


def test_forged_generation_or_desired_metadata_cannot_pass_cas() -> None:
    store = MemoryJournalStore()
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 30))
    forged = replace(claimed, generation=claimed.generation + 1)
    with pytest.raises(StoreConflict):
        store.compare_and_swap(forged, Transition(Progress()))


def test_hash_canonicalizes_secret_references_and_includes_immutable_input() -> None:
    first, second = (
        SecretReference("db-password", "version-7"),
        SecretReference("tls", "version-2"),
    )
    desired = replace(spec().desired, secrets=(first, second))
    reordered = replace(desired, secrets=(second, first))
    changed = replace(desired, approved_input=InputReference("b" * 64))
    assert desired.desired_hash == reordered.desired_hash
    assert desired.desired_hash != changed.desired_hash
    assert desired.desired_hash.startswith("pywhales.desired.v1:sha256:")


def test_duplicate_secret_names_are_rejected() -> None:
    with pytest.raises(InvalidDescription):
        replace(
            spec().desired,
            secrets=(SecretReference("db", "v1"), SecretReference("db", "v2")),
        )


@pytest.mark.parametrize(
    "digest", ["secret-value", "A" * 64, "a" * 63, "https://user:password@example.com"]
)
def test_approved_input_requires_digest_not_payload_or_location(digest: str) -> None:
    with pytest.raises(InvalidDescription) as raised:
        InputReference(digest)
    assert digest not in repr(raised.value)


def test_ownership_labels_use_agreed_storage_namespace() -> None:
    assert ownership_labels(spec()) == {
        "io.pywhales.storage.v1.endpoint": "manager-a",
        "io.pywhales.storage.v1.owner": "team-a",
        "io.pywhales.storage.v1.class": "ephemeral",
        "io.pywhales.storage.v1.operation": "deploy-42",
    }


def test_read_missing_entry_does_not_create_it() -> None:
    store = MemoryJournalStore()
    result = store.read(spec().key)
    assert result is None
    assert store.create_if_absent(spec()) == JournalEntry(
        spec(), spec().desired.desired_hash
    )


def test_deadline_summary_round_trips_and_is_generation_fenced() -> None:
    clock = Clock()
    store = MemoryJournalStore(clock)
    detail = DeadlineDetail(
        2,
        missing_ready_slots=1,
        duplicate_slots=2,
        stale_templates=1,
        non_running_tasks=2,
        invalid_task_shape=1,
        ongoing_updates=2,
        unfinished_completion_containers=1,
        restart_budget_exceeded_containers=1,
        non_ready_nodes=1,
    )
    claimed = store.claim(store.create_if_absent(spec()), LeaseRequest("worker-a", 5))
    progress = Progress(
        AttemptState.OBSERVING, 1, convergence="deadline", deadline_detail=detail
    )
    published = store.compare_and_swap(claimed, Transition(progress, release=True))
    assert store.read(spec().key) == published
    assert asdict(published.progress)["deadline_detail"] == {
        "captured_services": 2,
        "missing_ready_slots": 1,
        "duplicate_slots": 2,
        "stale_templates": 1,
        "non_running_tasks": 2,
        "invalid_task_shape": 1,
        "ongoing_updates": 2,
        "missing_ready_nodes": 0,
        "duplicate_node_assignments": 0,
        "missing_node_assignments": 0,
        "outside_approved_nodes": 0,
        "non_running_containers": 0,
        "restarting_containers": 0,
        "paused_containers": 0,
        "unhealthy_containers": 0,
        "unfinished_completion_containers": 1,
        "restart_budget_exceeded_containers": 1,
        "non_ready_nodes": 1,
        "unavailable_nodes": 0,
        "non_manager_nodes": 0,
        "unreachable_managers": 0,
    }
    stale = store.claim(published, LeaseRequest("worker-a", 5))
    clock.now += 5
    current = store.claim(stale, LeaseRequest("worker-b", 5))
    replacement = replace(
        progress, deadline_detail=DeadlineDetail(2, ongoing_updates=1)
    )
    updated = store.compare_and_swap(current, Transition(replacement, release=True))
    with pytest.raises(StoreConflict):
        store.compare_and_swap(stale, Transition(progress))
    assert store.read(spec().key) == updated


@pytest.mark.parametrize(
    "state,convergence",
    [(AttemptState.FAILED, "deadline"), (AttemptState.OBSERVING, None)],
)
def test_progress_rejects_obsolete_deadline_detail(
    state: AttemptState, convergence: ConvergenceSummary | None
) -> None:
    with pytest.raises(InvalidDescription):
        replace(
            Progress(),
            state=state,
            convergence=convergence,
            deadline_detail=DeadlineDetail(1),
        )


@pytest.mark.parametrize("subclass", [False, True])
def test_progress_rejects_payload_bearing_deadline_detail(subclass: bool) -> None:
    @dataclass(frozen=True, slots=True)
    class PayloadDetail(DeadlineDetail):
        wire: dict[str, str] = field(default_factory=dict)

    payload = {"secret": "credential-payload"}
    detail = PayloadDetail(1, wire=payload) if subclass else payload
    with pytest.raises(InvalidDescription):
        replace(
            Progress(),
            state=AttemptState.OBSERVING,
            convergence="deadline",
            deadline_detail=detail,
        )
