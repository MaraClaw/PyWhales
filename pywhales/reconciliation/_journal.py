"""Immutable journal state and generation-checked transition values."""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pywhales.convergence import DeadlineDetail
from pywhales.inventory import InventoryKind
from pywhales.reconciliation._errors import InvalidDescription
from pywhales.reconciliation._intent import OperationSpec, ResourceClass, token


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Immutable resource identity with observed storage-v1 ownership metadata.

    Adapters must inspect exact IDs and labels, never infer ownership from names
    or Docker's unused state. Labels are metadata, not authorization.
    """

    kind: InventoryKind
    identity: str
    endpoint: str
    owner: str
    resource_class: ResourceClass
    operation: str

    def __post_init__(self) -> None:
        token(self.identity)
        for value in (self.endpoint, self.owner, self.operation):
            token(value, max_length=64)


class AttemptState(StrEnum):
    """Persisted attempt state; UNKNOWN includes pre-launch intent and crashes."""

    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    OBSERVING = "observing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


type ConvergenceSummary = Literal[
    "success",
    "deadline",
    "cancelled",
    "unsupported",
    "malformed",
    "superseded",
    "update_failed",
    "completion_failed",
    "exited",
    "observation",
]


@dataclass(frozen=True, slots=True)
class Progress:
    """Closed decision state with optional past complete-cycle deadline counts.

    The integer-only detail excludes convergence causes, raw states and payloads.
    It is present only for a nonterminal deadline, never a current-state guarantee.
    """

    state: AttemptState = AttemptState.SUBMITTED
    attempts: int = 0
    resources: tuple[ResourceIdentity, ...] = ()
    convergence: ConvergenceSummary | None = None
    deadline_detail: DeadlineDetail | None = None

    def __post_init__(self) -> None:
        """Reject extended aggregates or detail on a different progress outcome."""
        if self.deadline_detail is not None and (
            type(self.deadline_detail) is not DeadlineDetail
            or self.convergence != "deadline"
            or self.state is not AttemptState.OBSERVING
        ):
            raise InvalidDescription()


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    """Unique worker incarnation token and positive lease duration in seconds."""

    worker: str
    ttl: float

    def __post_init__(self) -> None:
        token(self.worker)
        if isinstance(self.ttl, bool) or not math.isfinite(self.ttl) or self.ttl <= 0:
            raise InvalidDescription()


@dataclass(frozen=True, slots=True)
class Lease:
    """Store-clock deadline; generation belongs to the enclosing journal entry."""

    worker: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Versioned immutable intent and current fencing generation.

    Keep this tombstone for as long as operation identities can be reused.
    Deleting it destroys deduplication and monotonic fencing history.
    """

    spec: OperationSpec
    desired_hash: str
    version: int = 0
    generation: int = 0
    lease: Lease | None = None
    progress: Progress = Progress()


@dataclass(frozen=True, slots=True)
class Transition:
    """Replacement progress; releasing also invalidates the current worker."""

    progress: Progress
    release: bool = False
