"""Context-local launch correlation without importing admission policy."""

from contextvars import ContextVar
from typing import Final

from ._records import AdmissionId

current_admission_id: Final[ContextVar[AdmissionId | None]] = ContextVar(
    "telemetry_admission_id", default=None
)
