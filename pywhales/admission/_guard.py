"""Context-local reentry protection without a global owner registry."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from pywhales.telemetry import AdmissionId
from pywhales.telemetry._context import current_admission_id

from ._errors import AdmissionReentryError

_owners: Final[ContextVar[tuple[int, ...]]] = ContextVar("admission_owners", default=())


def check_reentry(owner: int) -> None:
    if owner in _owners.get():
        raise AdmissionReentryError()


@contextmanager
def operation_scope(owner: int, admission_id: AdmissionId | None) -> Generator[None]:
    """Mark user operation execution; cross-thread nesting is unsupported."""
    token = _owners.set((*_owners.get(), owner))
    admission_token = current_admission_id.set(admission_id)
    try:
        yield
    finally:
        current_admission_id.reset(admission_token)
        _owners.reset(token)
