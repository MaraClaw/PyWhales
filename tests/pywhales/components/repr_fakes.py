"""Shared launch guards and handle states for resource repr tests."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from pywhales._observation import observation
from pywhales.client_config import ReloadableObject
from pywhales.inventory import InspectionPolicy


@contextmanager
def without_process_launches() -> Generator[None]:
    """Fail on a launch, even if the code under test catches the exception."""
    error = AssertionError("resource representation attempted to launch a process")
    # PyWhales also binds Popen by name, so patch those references too.
    targets = (
        "subprocess.Popen",
        "subprocess.run",
        "pywhales.utils.Popen",
        "pywhales.utils._run_isolated",
        "pywhales.components.image.cli_wrapper.Popen",
    )
    with ExitStack() as stack:
        guards = [
            stack.enter_context(patch(target, side_effect=error)) for target in targets
        ]
        yield
        for guard in guards:
            guard.assert_not_called()


def assert_handle_reprs[SnapshotT, ReferenceT: str | None](
    handle: ReloadableObject[SnapshotT, ReferenceT],
    snapshot: SnapshotT,
    *,
    lazy: str,
    loaded: str,
    partial: str,
) -> None:
    """Check lazy, expired, pinned, and uninitialized reprs without Docker."""
    policy = InspectionPolicy(
        max_references=1,
        max_argv_bytes=4096,
        max_response_bytes=4096,
        max_total_launch_attempts=1,
    )
    with (
        without_process_launches(),
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 0),
        observation(policy) as state,
    ):
        assert repr(handle) == lazy

        handle._set_inspect_result(snapshot)
        assert handle._needs_reload()
        assert repr(handle) == loaded

        handle._set_pinned_inspect_result(snapshot)
        assert repr(handle) == loaded

        assert repr(object.__new__(type(handle))) == partial
        state.check_launch_attempts(1)
