"""Launch preflight and native-limit mapping shared by observed subprocesses."""

import errno
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager

from pywhales._launch_limits import argv_size, check_launch
from pywhales._observation import ObservationState, check_observation
from pywhales.exceptions import ObservationLimitError


def check_scoped_launch(
    args: Sequence[str], env: Mapping[str, str], observation: ObservationState
) -> None:
    """Preflight and reserve one launch before any process starts.

    Cancellation and expiry are checked on both sides of the size limits, so a
    scope that ended while the limits were measured never launches.
    Only then reserve against every enclosing launch account atomically. Later
    interruption, spawn failure, or CLI failure never refunds that reservation.

    Args:
        args: Complete stringified command.
        env: Effective child environment, including runner-added entries.
        observation: Scope whose bounds and tokens apply to this launch.

    Raises:
        ObservationCancelled: If any active token is set.
        ObservationTimeout: If the scope deadline has passed.
        ObservationLimitError: If argv, environment, or launch attempts exceed a limit.
    """
    check_observation(observation)
    check_launch(args, env, observation.policy)
    check_observation(observation)
    observation.check_launch_attempts(1, reserve=True)


@contextmanager
def environment_limit(observed: bool) -> Generator[None]:
    """Report an invalid launch environment as an observation limit when observed.

    Args:
        observed: Whether an observation scope is active for the launch.

    Raises:
        ObservationLimitError: If ``observed`` and the block rejects the
            environment with ``TypeError`` or ``ValueError``; the original error
            is dropped so no entry contents are exposed. Otherwise it propagates.
    """
    try:
        yield
    except TypeError, ValueError:
        if observed:
            raise ObservationLimitError("environment", None, None) from None
        raise


@contextmanager
def native_argv_limit(args: Sequence[str]) -> Generator[None]:
    """Report an operating-system argument-size refusal as an observation limit.

    Args:
        args: Command whose launch is attempted inside the block.

    Raises:
        ObservationLimitError: If the launch fails with ``E2BIG`` (or its
            Windows equivalent); other ``OSError`` values propagate unchanged.
    """
    try:
        yield
    except OSError as error:
        if error.errno == errno.E2BIG or getattr(error, "winerror", None) == 206:
            raise ObservationLimitError("native_argv", None, argv_size(args)) from None
        raise
