"""Linearizable journal-store protocol and NONdurable conformance implementation."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock
from time import monotonic
from typing import Literal, Protocol

from pywhales.reconciliation._errors import IdentityConflict, StoreConflict
from pywhales.reconciliation._intent import OperationKey, OperationSpec
from pywhales.reconciliation._journal import (
    AttemptState,
    JournalEntry,
    Lease,
    LeaseRequest,
    Transition,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreSemantics:
    """Adapter's explicit deployment contract, not a backend qualification claim.

    Document actual commit/crash durability, transaction boundary, authoritative
    clock and skew assumptions, tombstone retention, and restore/recovery envelope.
    A durable adapter must preserve versions and generations through restart.
    """

    durability: Literal["none", "durable"]
    transaction_boundary: str
    clock: str
    retention: str
    recovery: str


class JournalStore(Protocol):
    """Atomic create/read/CAS contract across every controller sharing the store.

    Every write checks version and generation together. Claim increments both;
    CAS, renew and release increment version and require an unexpired matching
    worker lease using store-authoritative time. Renew moves only that lease's
    expiry. Verify is a linearizable read/check.
    Conflicts raise StoreConflict (identity mismatch raises IdentityConflict).
    Store failures raise payload-free StoreUnavailable, even for ambiguous commits;
    callers stop actions and reread later, never assume a failed write rolled back.
    Backend exceptions must be suppressed with ``from None`` at the adapter edge.
    Attempts never decrease, and progressed entries cannot revert to SUBMITTED.
    Operation tokens are unique across the store, not reusable per endpoint.
    """

    @property
    def semantics(self) -> StoreSemantics:
        """Return declared backend semantics before deployment."""
        ...

    def create_if_absent(self, spec: OperationSpec) -> JournalEntry:
        """Create once or return exact matching intent; conflicting intent fails."""
        ...

    def read(self, key: OperationKey) -> JournalEntry | None:
        """Read the current entry without creating it."""
        ...

    def claim(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        """CAS an absent/expired lease, incrementing version and generation."""
        ...

    def verify(self, expected: JournalEntry) -> None:
        """Require exact version, generation, owner and unexpired lease."""
        ...

    def renew(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        """CAS the same worker's unexpired lease to a fresh TTL, incrementing version."""
        ...

    def compare_and_swap(
        self, expected: JournalEntry, change: Transition
    ) -> JournalEntry:
        """Commit progress under current lease/version/generation, optionally releasing."""
        ...


class MemoryJournalStore:
    """Transactional thread-safe fake; NONdurable, not shared across processes.

    Restarting a Controller with the same object tests recovery control flow only.
    This is not process-crash recovery, a disk journal or a production backend.
    """

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        """Use a process-monotonic or deterministic test clock owned by this store."""
        self._clock = clock
        self._entries: dict[str, JournalEntry] = {}
        self._lock = RLock()

    @property
    def semantics(self) -> StoreSemantics:
        """Declare volatile, per-instance lock serialization and no crash recovery."""
        return StoreSemantics(
            durability="none",
            transaction_boundary="one method under one instance lock",
            clock="injected process-monotonic seconds; no cross-host clock",
            retention="until object is discarded; no deletion API",
            recovery="controller restart only while this same store object survives",
        )

    def create_if_absent(self, spec: OperationSpec) -> JournalEntry:
        """Atomically return matching intent or create its initial journal entry."""
        with self._lock:
            current = self._entries.get(spec.key.operation)
            if current is not None:
                if (
                    current.spec != spec
                    or current.desired_hash != spec.desired.desired_hash
                ):
                    raise IdentityConflict()
                return current
            entry = JournalEntry(spec, spec.desired.desired_hash)
            self._entries[spec.key.operation] = entry
            return entry

    def read(self, key: OperationKey) -> JournalEntry | None:
        """Return an immutable snapshot under the instance lock."""
        with self._lock:
            current = self._entries.get(key.operation)
            if current is not None and current.spec.key != key:
                raise IdentityConflict()
            return current

    def _current(self, expected: JournalEntry) -> JournalEntry:
        current = self._entries.get(expected.spec.key.operation)
        if current is None or current != expected:
            raise StoreConflict()
        return current

    def claim(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        """Acquire only absent/expired leases, fencing even a repeated worker token."""
        with self._lock:
            current = self._current(expected)
            now = self._clock()
            if current.lease is not None and current.lease.expires_at > now:
                raise StoreConflict()
            claimed = replace(
                current,
                version=current.version + 1,
                generation=current.generation + 1,
                lease=Lease(request.worker, now + request.ttl),
            )
            self._entries[current.spec.key.operation] = claimed
            return claimed

    def verify(self, expected: JournalEntry) -> None:
        """Reject a stale snapshot or expired lease without changing state."""
        with self._lock:
            current = self._current(expected)
            if current.lease is None or current.lease.expires_at <= self._clock():
                raise StoreConflict()

    def renew(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        """Move a live lease's expiry to store time plus TTL, bumping only version.

        Args:
            expected: Exact current entry, as returned by the claim or the latest
                renewal or commit.
            request: Worker that must own the lease, and the TTL in seconds from
                the store clock, validated when the request is built.

        Returns:
            The entry with a new version and expiry; generation, progress and
            worker are unchanged.

        Raises:
            StoreConflict: The entry is stale, released or expired, or its lease
                belongs to another worker.
        """
        with self._lock:
            current = self._current(expected)
            now = self._clock()
            lease = current.lease
            if (
                lease is None
                or lease.worker != request.worker
                or lease.expires_at <= now
            ):
                raise StoreConflict()
            renewed = replace(
                current,
                version=current.version + 1,
                lease=Lease(lease.worker, now + request.ttl),
            )
            self._entries[current.spec.key.operation] = renewed
            return renewed

    def compare_and_swap(
        self, expected: JournalEntry, change: Transition
    ) -> JournalEntry:
        """Replace progress atomically under an exact live fencing generation."""
        with self._lock:
            self.verify(expected)
            if change.progress.attempts < expected.progress.attempts or (
                expected.progress.state != change.progress.state
                and change.progress.state is AttemptState.SUBMITTED
            ):
                raise StoreConflict()
            updated = replace(
                expected,
                version=expected.version + 1,
                progress=change.progress,
                lease=None if change.release else expected.lease,
            )
            self._entries[expected.spec.key.operation] = updated
            return updated
