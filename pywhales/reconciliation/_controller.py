"""Optional fenced reconciliation passes; no scheduler or automatic mutation retry."""

from dataclasses import replace
from typing import assert_never

from pywhales.convergence import Cancelled, Deadline, Failure, Outcome, Success
from pywhales.exceptions import DockerException
from pywhales.reconciliation._adapters import (
    ActionContext,
    AdmittedReconciliation,
    InventoryDisposition,
    InventoryEvidence,
    ReconciliationAdapters,
)
from pywhales.reconciliation._errors import (
    AdapterUnavailable,
    AmbiguousMutation,
    IdentityConflict,
    StoreConflict,
    StoreUnavailable,
)
from pywhales.reconciliation._intent import OperationSpec
from pywhales.reconciliation._journal import (
    AttemptState,
    JournalEntry,
    LeaseRequest,
    Progress,
    Transition,
)
from pywhales.reconciliation._store import JournalStore


def _outcome(progress: Progress, outcome: Outcome) -> Progress:
    match outcome:
        case Success():
            return replace(
                progress,
                state=AttemptState.SUCCEEDED,
                convergence="success",
                deadline_detail=None,
            )
        case Failure(reason=reason):
            return replace(
                progress,
                state=AttemptState.FAILED,
                convergence=reason,
                deadline_detail=None,
            )
        case Deadline(detail=detail):
            return replace(
                progress,
                state=AttemptState.OBSERVING,
                convergence="deadline",
                deadline_detail=detail,
            )
        case Cancelled():
            return replace(
                progress,
                state=AttemptState.OBSERVING,
                convergence="cancelled",
                deadline_detail=None,
            )
        case unreachable:
            assert_never(unreachable)


class _ObservedLease:
    """Latest journal entry of one observation, advanced only by its renewals.

    Renewal bumps the entry version, so checks, further renewals and the final
    commit must all use the entry returned by the last successful renewal. A
    failed renewal may still have committed, so it halts this object for good:
    every later access raises the same failure instead of reusing a prior entry.
    """

    def __init__(self, store: JournalStore, entry: JournalEntry) -> None:
        """Track ``entry``, the unreleased OBSERVING commit that holds the lease."""
        self._store = store
        self._entry = entry
        self._halt: StoreConflict | StoreUnavailable | None = None

    @property
    def entry(self) -> JournalEntry:
        """Return the latest entry, or raise the failure that halted renewal."""
        if self._halt is not None:
            raise self._halt
        return self._entry

    def check_current(self) -> None:
        """Verify the latest entry against the store."""
        self._store.verify(self.entry)

    def renew(self, ttl: float) -> None:
        """Renew the latest entry's worker lease, adopting the renewed entry."""
        entry = self.entry
        lease = entry.lease
        if lease is None:
            raise StoreConflict()
        try:
            self._entry = self._store.renew(entry, LeaseRequest(lease.worker, ttl))
        except (StoreConflict, StoreUnavailable) as error:
            self._halt = error
            raise


class Controller:
    """Reconcile one named operation per call using explicitly supplied adapters.

    Call submit to create/read intent, claim to acquire a worker lease, then
    reconcile. Schedule passes periodically as well as on non-durable event hints.
    Every pass inventories, including previously successful operations. A completed
    pass releases its lease; interrupted work is recovered after lease expiry.
    A convergence adapter may renew the lease through its context while observing;
    the outcome commit and release then use the renewed entry.
    Store conflicts/unavailability, including failed renewals, halt the pass
    without further adapter actions or an outcome commit.
    """

    def __init__(
        self,
        store: JournalStore,
        adapters: ReconciliationAdapters,
        *,
        admission: AdmittedReconciliation | None = None,
    ) -> None:
        """Bind a store and endpoint; optionally defer each pass behind admission."""
        if (
            admission is not None
            and admission.owner.snapshot().endpoint != adapters.endpoint
        ):
            raise IdentityConflict()
        self.store = store
        self.adapters = adapters
        self.admission = admission

    def _endpoint(self, spec: OperationSpec) -> None:
        if spec.key.endpoint != self.adapters.endpoint:
            raise IdentityConflict()

    def submit(self, spec: OperationSpec) -> JournalEntry:
        """Create intent once, returning matching existing state without Docker calls."""
        self._endpoint(spec)
        return self.store.create_if_absent(spec)

    def claim(self, expected: JournalEntry, request: LeaseRequest) -> JournalEntry:
        """Acquire a conditional lease; an active lease cannot be taken over early."""
        self._endpoint(expected.spec)
        return self.store.claim(expected, request)

    def reconcile(self, claimed: JournalEntry) -> JournalEntry:
        """Inventory and advance one leased operation, without replaying uncertainty.

        Admission queues before the pass; lease checks happen inside the deferred
        callback and before every adapter call. Unknown is persisted before launch,
        so a crash, lost response or post-launch store outage cannot enable replay.
        Only a never-attempted submission with authoritative ABSENT evidence can
        mutate. After any attempt, absent/incomplete evidence remains unknown and
        requires operator resolution, not an automatic retry under this identity.

        Raises:
            StoreConflict: The version or worker lease is no longer current.
            StoreUnavailable: Store access failed; stop new decisions and reread later.
            AdapterUnavailable: Observation failed; no usable evidence was published.
            IdentityConflict: Endpoint or observed ownership contradicts intent.
        """
        self._endpoint(claimed.spec)
        if self.admission is not None:
            return self.admission.owner.request(
                self.admission.pressure, self.admission.queue_timeout
            ).call(lambda: self._pass(claimed))
        return self._pass(claimed)

    def _context(self, entry: JournalEntry) -> ActionContext:
        self.store.verify(entry)
        return ActionContext(entry.spec, lambda: self.store.verify(entry))

    def _inventory(self, entry: JournalEntry) -> InventoryEvidence:
        context = self._context(entry)
        try:
            return self.adapters.inventory.inventory(context)
        except DockerException, OSError:
            raise AdapterUnavailable() from None

    def _commit(
        self, entry: JournalEntry, progress: Progress, *, release: bool = True
    ) -> JournalEntry:
        return self.store.compare_and_swap(entry, Transition(progress, release))

    def _pass(self, entry: JournalEntry) -> JournalEntry:
        evidence = self._inventory(entry)
        match evidence.disposition:
            case InventoryDisposition.CONFLICT:
                raise IdentityConflict()
            case InventoryDisposition.INDETERMINATE:
                state = (
                    AttemptState.UNKNOWN
                    if entry.progress.attempts
                    or entry.progress.state != AttemptState.SUBMITTED
                    else AttemptState.SUBMITTED
                )
                return self._commit(
                    entry,
                    replace(
                        entry.progress,
                        state=state,
                        convergence=None,
                        deadline_detail=None,
                    ),
                )
            case InventoryDisposition.APPLIED:
                return self._observe(entry, evidence)
            case InventoryDisposition.ABSENT:
                if evidence.resources:
                    raise AdapterUnavailable()
                if (
                    entry.progress.attempts
                    or entry.progress.state != AttemptState.SUBMITTED
                ):
                    return self._commit(
                        entry,
                        replace(
                            entry.progress,
                            state=AttemptState.UNKNOWN,
                            convergence=None,
                            deadline_detail=None,
                        ),
                    )
                intent = self._commit(
                    entry, Progress(AttemptState.UNKNOWN, 1), release=False
                )
                context = self._context(intent)
                try:
                    self.adapters.mutation.apply(context)
                except AmbiguousMutation, DockerException, OSError:
                    return self._commit(intent, intent.progress)
                return self._pass(intent)
            case unreachable:
                assert_never(unreachable)

    def _observe(
        self, entry: JournalEntry, evidence: InventoryEvidence
    ) -> JournalEntry:
        spec = entry.spec
        if not evidence.resources or len(
            {(item.kind, item.identity) for item in evidence.resources}
        ) != len(evidence.resources):
            raise AdapterUnavailable()
        for item in evidence.resources:
            if (item.endpoint, item.owner, item.resource_class, item.operation) != (
                spec.key.endpoint.alias,
                spec.desired.owner,
                spec.desired.resource_class,
                spec.key.operation,
            ):
                raise IdentityConflict()
        observing = self._commit(
            entry,
            replace(
                entry.progress,
                state=AttemptState.OBSERVING,
                resources=evidence.resources,
                convergence=None,
                deadline_detail=None,
            ),
            release=False,
        )
        lease = _ObservedLease(self.store, observing)
        lease.check_current()
        context = ActionContext(spec, lease.check_current, lease.renew)
        try:
            outcome = self.adapters.convergence.observe(context, evidence.resources)
        except DockerException, OSError:
            raise AdapterUnavailable() from None
        latest = lease.entry
        return self._commit(latest, _outcome(latest.progress, outcome))
