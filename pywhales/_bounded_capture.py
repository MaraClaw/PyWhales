"""Bounded, interruptible capture of a single local subprocess."""

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from time import monotonic

from pywhales._capture_pipes import CapturePipes
from pywhales._execution_policy import ExecutionPolicy
from pywhales._observation import ObservationState, check_observation
from pywhales._readiness import CANCEL_CHECK, watch_exit
from pywhales._scoped_launch import check_scoped_launch, native_argv_limit
from pywhales._stdin_pump import StdinPump
from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
)
from pywhales.telemetry._lifecycle import ExecutionTelemetry


def _launch(
    args: list[str],
    env: Mapping[str, str],
    pass_fds: Sequence[int],
    *,
    piped_stdin: bool,
) -> subprocess.Popen[bytes]:
    """Start the child with piped output, and piped stdin only for supplied input."""
    with native_argv_limit(args):
        if piped_stdin:
            return subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                pass_fds=pass_fds,
            )
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            pass_fds=pass_fds,
        )


def bounded_capture(
    args: list[str],
    env: Mapping[str, str],
    pass_fds: Sequence[int],
    timeout: float | None,
    policy: ExecutionPolicy,
    observation: ObservationState,
    *,
    telemetry: ExecutionTelemetry,
    input: bytes | Iterable[bytes] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Launch, capture within the observation budget, and reap the local child.

    Output is drained while ``input`` is uploaded by a writer thread, so a child
    that fills stdout or stderr before reading stdin cannot deadlock the call.
    Uploaded bytes are charged to the scope's stdin accounts; the caller supplies
    ``input`` only under such a budget.

    Args:
        args: Complete stringified command.
        env: Effective child environment.
        pass_fds: Descriptors kept open in the child.
        timeout: Positive finite seconds from launch, or ``None``.
        policy: Execution policy snapshot for diagnostics.
        observation: Scope whose bounds and tokens apply to this launch.
        telemetry: Measurements for this launch.
        input: Bytes, or an iterable of byte chunks pulled lazily on the writer
            thread, sent to child stdin. ``None`` inherits stdin. A deadline or
            cancellation cannot interrupt the iterator's own code, so cleanup
            waits for a chunk in flight.

    Returns:
        The reaped child's return code with all captured output.

    Raises:
        ObservationLimitError: Output, argv, or environment exceeded a limit, or
            the input exceeded a stdin budget (reason ``total_stdin``).
        DockerTimeout: The selected timeout expired first.
        ObservationTimeout: The scope deadline expired.
        ObservationCancelled: A cancellation token was set.
    """
    with telemetry.preflight():
        reservation = check_scoped_launch(args, env, observation)
    try:
        telemetry.start()
        deadline = None if timeout is None else monotonic() + timeout
        process = _launch(args, env, pass_fds, piped_stdin=input is not None)
    except BaseException:
        reservation.release()
        raise

    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    read_bytes = {process.stdout.fileno(): 0, process.stderr.fileno(): 0}
    try:
        buffers = {process.stdout.fileno(): stdout, process.stderr.fileno(): stderr}
        with (
            watch_exit(process, reservation.release) as exited,
            CapturePipes((process.stdout, process.stderr), exited) as pipes,
            StdinPump(process, input, telemetry, observation=observation),
        ):
            while True:
                if any(token.is_set() for token in observation.cancel_tokens):
                    raise ObservationCancelled()
                now = monotonic()
                if exited.is_set():
                    # A natural exit ends the selected timeout; scopes still apply.
                    deadline = None
                if (
                    deadline is not None
                    and now >= deadline
                    and (
                        observation.deadline is None or deadline < observation.deadline
                    )
                ):
                    process.kill()
                    process.wait()
                    raise DockerTimeout(
                        args,
                        process.returncode,
                        bytes(stdout),
                        bytes(stderr),
                        raw_diagnostics=policy.raw_diagnostics,
                    ) from None
                check_observation(observation)
                if not pipes and exited.is_set():
                    break
                # Wait for pipe data or child exit; only a deadline or a cancel
                # token (a plain Event that cannot wake the wait) bounds it.
                wait = None
                for end in (deadline, observation.deadline):
                    if end is not None:
                        remaining = max(0, end - now)
                        wait = remaining if wait is None else min(wait, remaining)
                if observation.cancel_tokens:
                    wait = CANCEL_CHECK if wait is None else min(wait, CANCEL_CHECK)
                for fd, available in pipes.ready(wait):
                    if available == 0:
                        pipes.discard(fd)
                        continue
                    remaining = (
                        observation.policy.max_response_bytes
                        - len(stdout)
                        - len(stderr)
                    )
                    allowance = observation.response_allowance(remaining)
                    chunk = os.read(fd, min(available, allowance + 1, 65536))
                    read_bytes[fd] += len(chunk)
                    if chunk:
                        observation.charge_response(len(chunk))
                    if not chunk:
                        pipes.discard(fd)
                    elif len(chunk) > remaining:
                        raise ObservationLimitError(
                            "response",
                            observation.policy.max_response_bytes,
                            observation.policy.max_response_bytes + 1,
                        )
                    else:
                        buffers[fd].extend(chunk)
        check_observation(observation)
        return subprocess.CompletedProcess(
            args, process.wait(), bytes(stdout), bytes(stderr)
        )
    finally:
        if process.poll() is None:
            process.kill()
        if process.stdin is not None:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        process.wait()
        reservation.release()
        telemetry.return_code = process.returncode
        telemetry.stdout_bytes, telemetry.stderr_bytes = read_bytes.values()
