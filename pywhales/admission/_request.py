"""Single-use deferred admission handles without retained user operations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from ._contracts import validate_demand
from ._guard import operation_scope
from ._state import Bucket, Outcome, Ticket
from ._stream import AdmittedStream

if TYPE_CHECKING:
    from ._owner import AdmissionOwner


class AdmissionRequest:
    """Inert single-use handle; cancellation only applies before active admission."""

    def __init__(
        self,
        owner: AdmissionOwner,
        bucket: Bucket,
        timeout: float | None,
        *,
        demand: int = 1,
    ) -> None:
        """Bind an unregistered ticket to the owner's selected class."""
        validate_demand(demand, bucket.limit.demand_capacity)
        self._owner = owner
        self._bucket = bucket
        self._timeout = timeout
        self._ticket = Ticket(demand=demand)

    def try_resize(self, demand: int) -> bool:
        """Atomically try to replace this active reservation without waiting.

        Shrinking wakes FIFO waiters. Growth returns False, unchanged, when demand
        capacity is unavailable or any request is pending in this class. Equal
        demand succeeds. Owner closure alone does not end active work.

        Args:
            demand: Positive integer total reservation, not a delta.

        Returns:
            Whether the reservation was resized, without releasing its active slot.

        Raises:
            ValueError: If demand is invalid or exceeds the class capacity.
            RuntimeError: Before admission, after release, or once stream cleanup
                begins, including through this original request handle.
        """
        return self._owner._try_resize(self, demand)

    def call[T](self, operation: Callable[[], T]) -> T:
        """Admit then invoke work once, retaining capacity through BaseException.

        SDK preflights and children must be inside operation and called directly,
        not admitted again through this owner. No active cancellation or rollback
        of daemon-side work is provided. Operation failures retain their identity.
        """
        self._owner._acquire(self)
        outcome = Outcome.FAILED
        try:
            with operation_scope(id(self._owner), self._ticket.admission_id):
                result = operation()
            outcome = Outcome.COMPLETED
            return result
        finally:
            self._owner._release(self, outcome)

    def cancel(self) -> bool:
        """Atomically cancel before active admission; return whether this call won."""
        return self._owner._cancel(self)

    def stream[T](
        self, factory: Callable[[], AbstractContextManager[Iterator[T]]]
    ) -> AdmittedStream[T]:
        """Admit before factory invocation and source entry, then own its context.

        Factory and failed entry own partial-resource cleanup. One outer permit
        covers all direct SDK children, including composite image streams. Source
        lifecycle behavior is delegated, not replaced or retrofitted into the SDK.
        """
        self._owner._acquire(self)
        try:
            with operation_scope(id(self._owner), self._ticket.admission_id):
                source = factory()
                iterator = source.__enter__()
        except BaseException:
            self._owner._release(self, Outcome.FAILED)
            raise
        return AdmittedStream(self, source, iterator)
