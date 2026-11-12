"""Shared reclamation of stream cores whose public handles were collected.

Owned streams split into a public handle and an internal core. Worker threads
and callbacks reference only the core, so an abandoned handle can become
unreachable while its local CLI still runs. Its finalizer then puts the core on
a queue that is safe to use from weakref callbacks; it takes no locks, starts
no threads and runs no cleanup, because collection can happen on any thread,
including one inside a stream or admission critical section.

One daemon thread, started from ordinary code when the first handle is
watched, takes each core and runs its explicit ``close()`` path in a fresh
context. Finalizers don't run at interpreter exit, and the reclaimer never
blocks it. Importing this module starts nothing.
"""

from __future__ import annotations

import logging
from contextvars import Context
from queue import SimpleQueue
from threading import Lock, Thread
from typing import Protocol
from weakref import finalize

LOGGER = logging.getLogger(__name__)


class StreamCore(Protocol):
    """Internal stream state whose idempotent close releases owned resources."""

    def close(self) -> None:
        """Release owned resources through the explicit close path."""


_pending: SimpleQueue[StreamCore] = SimpleQueue()
_start_lock = Lock()
_worker: Thread | None = None


def watch(handle: object, core: StreamCore) -> None:
    """Close ``core`` on the shared reclaimer once ``handle`` is collected.

    Call this from ordinary code, normally the handle's constructor: it starts
    the reclaimer thread if needed. The registered finalizer only enqueues the
    core and is skipped at interpreter exit. Closing a stream explicitly first
    is harmless, because reclamation reuses the idempotent close path.

    Args:
        handle: Public stream object the caller holds. The core must not
            reference it, directly or through its threads and callbacks.
        core: Internal state to close after the handle becomes unreachable.
    """
    _ensure_worker()
    finalize(handle, _pending.put, core).atexit = False


def _ensure_worker() -> None:
    """Start the reclaimer daemon unless one is already running."""
    global _worker
    with _start_lock:
        if _worker is None or not _worker.is_alive():
            worker = Thread(target=_reclaim_forever, name="pywhales-reclaimer")
            worker.daemon = True
            worker.start()
            _worker = worker


def _reclaim_forever() -> None:
    """Close queued cores one at a time, logging failures without values.

    A failure is a recorded stream error or a cleanup error that a caller
    would otherwise have received. The reclaimer can't tell whether the
    originating client allows raw diagnostics, so it logs only the exception
    type and keeps running.
    """
    while True:
        core = _pending.get()
        try:
            Context().run(core.close)
        except BaseException as error:
            LOGGER.warning(
                "Reclaiming an abandoned stream raised %s; details withheld",
                type(error).__qualname__,
            )
        del core
