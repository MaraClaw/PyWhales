"""Subprocess execution, owned output adapters, and Docker value conversions.

Execution snapshots the originating client's policy before stringifying argv.
Safe automatic diagnostics omit command, environment, and captured payload
values, but returned data, raw exception attributes, and uncaptured terminal
output remain outside that boundary.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from subprocess import PIPE, Popen
from time import monotonic
from types import MappingProxyType, TracebackType
from typing import (
    Literal,
    Self,
    overload,
    override,
)

import pydantic

from pywhales._environment import finalize_environment, resolve_environment
from pywhales._execution_policy import (
    DEFAULT_BUFFER,
    BufferDefault,
    get_execution_policy,
)
from pywhales._process_stream import ProcessStream as ProcessStream
from pywhales._stdin_pump import StdinPump
from pywhales.exceptions import DockerException, DockerTimeout, ObservationLimitError
from pywhales.exceptions import get_docker_exception_type as get_docker_exception_type
from pywhales.telemetry._lifecycle import ExecutionTelemetry, outcome_of

PROJECT_ROOT = Path(__file__).parents[1]

LOGGER = logging.getLogger(__name__)


def custom_parse_object_as[T](type_: type[T], obj: object) -> T:
    """Parse a Python value using Pydantic's adapter for the requested type.

    Args:
        type_: Target model or type accepted by Pydantic's TypeAdapter.
        obj: Value to validate and convert.

    Returns:
        The parsed value using the target type's validation rules.

    Raises:
        pydantic.ValidationError: If the input cannot satisfy the target type.
    """
    return pydantic.TypeAdapter(type_).validate_python(obj)


def title_if_necessary(string: str) -> str:
    """Return all-uppercase text unchanged, otherwise apply title casing.

    Args:
        string: Name segment whose existing acronym capitalization is preserved.

    Returns:
        The unchanged uppercase segment or its title-cased form.
    """
    if string.isupper():
        return string
    else:
        return string.title()


def to_docker_camel(string: str) -> str:
    """Convert a field name to its Docker JSON alias with acronym exceptions.

    Args:
        string: Python field name, normally using underscore-separated words.

    Returns:
        The explicit Docker spelling for a known exception, or joined title-
        cased segments preserving segments that are already uppercase.
    """
    try:
        special_cases = {
            "exec_ids": "ExecIDs",
            "sandbox_id": "SandboxID",
            "oom_killed": "OOMKilled",
            "rw": "RW",
            "link_local_ipv6_address": "LinkLocalIPv6Address",
            "link_local_ipv6_prefix_length": "LinkLocalIPv6PrefixLen",
            "secondary_ipv6_addresses": "SecondaryIPv6Addresses",
            "endpoint_id": "EndpointID",
            "global_ipv6_prefix_length": "GlobalIPv6PrefixLen",
            "ip_prefix_length": "IPPrefixLen",
            "ipv6_gateway": "IPv6Gateway",
            "network_id": "NetworkID",
            "ip_address": "IPAddress",
            "global_ipv6_address": "GlobalIPv6Address",
            "blkio_device_read_iops": "BlkioDeviceReadIOps",
            "blkio_device_write_iops": "BlkioDeviceWriteIOps",
            "device_ids": "DeviceIDs",
            "kernel_memory_tcp": "KernelMemoryTCP",
            "container_id_file": "ContainerIDFile",
            "uts_mode": "UTSMode",
            "root_fs": "RootFS",
            "enable_ipv6": "EnableIPv6",
            "ipv4_address": "IPv4Address",
            "ipv6_address": "IPv6Address",
            "ipam": "IPAM",
            "ipam_config": "IPAMConfig",
            "tls_info": "TLSInfo",
            "virtual_ips": "VirtualIPs",
            "default_api_version": "DefaultAPIVersion",
            "min_api_version": "MinAPIVersion",
        }
        return special_cases[string]
    except KeyError:
        return "".join(title_if_necessary(x) for x in string.split("_"))


class DockerCamelModel(pydantic.BaseModel):
    """Metadata model accepting Python field names and generated Docker aliases.

    Alias generation preserves Docker-specific acronym spellings. Pydantic
    model building is deferred until needed.
    """

    model_config = pydantic.ConfigDict(
        populate_by_name=True,
        alias_generator=to_docker_camel,
        defer_build=True,
    )


def validate_timeout(timeout: float | None) -> None:
    """Require a positive, finite timeout or no deadline.

    Args:
        timeout: Duration in seconds; ``None`` disables the deadline.

    Raises:
        ValueError: If the value is boolean, nonfinite, zero, or negative.
    """
    if timeout is not None and (
        isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("timeout must be positive finite seconds or None")


def _debug_enabled() -> bool:
    """Return whether PYWHALES_DEBUG is exactly 1."""
    return os.environ.get("PYWHALES_DEBUG") == "1"


def _log_command(
    args: Sequence[str], env: Mapping[str, str], *, raw_diagnostics: bool
) -> None:
    """Emit command diagnostics without exposing values unless opted in.

    Args:
        args: Stringified arguments used for raw output or a safe argument count.
        env: Launch environment included only in raw debug printing.
        raw_diagnostics: Per-launch permission to expose confidential values.
            Debug environment variables enable printing but do not grant this
            permission; logger output follows the same value policy.
    """
    if _debug_enabled():
        print("------------------------------")
        if raw_diagnostics:
            print("command: " + " ".join(args))
            print(f"Env: {dict(env)}")
        else:
            print("command: [details withheld]")
            print("Env: [values withheld]")
        print("------------------------------")
    if raw_diagnostics:
        LOGGER.debug("Running command: %s", shlex.join(args))
    else:
        LOGGER.debug("Running command (%d arguments; details withheld)", len(args))


@overload
def run(
    args: Sequence[object],
    capture_stdout: bool = ...,
    capture_stderr: bool = ...,
    input: bytes | Iterable[bytes] | None = ...,
    return_stderr: Literal[False] = ...,
    env: Mapping[str, str] = ...,
    tty: bool = ...,
    pass_fds: Sequence[int] = ...,
    *,
    timeout: float | None = ...,
    workload_status: bool = ...,
) -> str: ...


@overload
def run(
    args: Sequence[object],
    capture_stdout: bool = ...,
    capture_stderr: bool = ...,
    input: bytes | Iterable[bytes] | None = ...,
    *,
    return_stderr: Literal[True],
    env: Mapping[str, str] = ...,
    tty: bool = ...,
    pass_fds: Sequence[int] = ...,
    timeout: float | None = ...,
    workload_status: bool = ...,
) -> tuple[str, str]: ...


def run(
    args: Sequence[object],
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    input: bytes | Iterable[bytes] | None = None,
    return_stderr: bool = False,
    env: Mapping[str, str] = MappingProxyType({}),
    tty: bool = False,
    pass_fds: Sequence[int] = (),
    *,
    timeout: float | None = None,
    workload_status: bool = False,
) -> str | tuple[str, str]:
    """Run a command synchronously and return decoded captured output.

    Snapshot the originating execution policy before converting arguments to
    strings. A timeout uses a monotonic deadline established immediately before
    launch, kills and reaps a still-running local CLI on expiry, and retains
    captured details on DockerTimeout. It does not guarantee cancellation of
    daemon-side work. Without a selected timeout, wait without a deadline.

    Inside ``observation()`` the call always uses bounded capture, which sends
    input only when an enclosing scope sets ``max_total_stdin_bytes``. Output is
    drained while input is uploaded, and the bytes the child's stdin pipe
    accepted are charged to every enclosing stdin account: a scope cannot be
    exceeded, an upload ending exactly at the limit succeeds, and one that needs
    more fails with reason ``total_stdin`` after the child is killed and reaped.
    The upload's writer thread is joined before the call returns or raises.

    Args:
        args: Executable and arguments, optionally retaining a policy carrier as
            the first item. Values are stringified without invoking a shell.
        capture_stdout: Capture stdout when true; otherwise inherit output.
            Ignored when tty is true.
        capture_stderr: Capture stderr when true; otherwise inherit output.
            Ignored when tty is true.
        input: Bytes, or an iterable of byte chunks, to send through child
            stdin; ``None`` inherits stdin and an empty value sends only EOF.
            Chunks are pulled lazily on a writer thread and never buffered
            whole. Deadlines and cancellation stop the child but cannot
            interrupt the iterator's own code, so cleanup waits for a chunk
            in flight. Inside an observation, input requires a stdin budget.
        return_stderr: Return both decoded streams when true instead of stdout
            alone. Uncaptured streams are represented by empty strings.
        env: Overrides applied after inherited and client environment layers.
        tty: Route output to current sys.stdout and sys.stderr instead of
            capturing it. This option does not allocate a pseudo-terminal.
        pass_fds: Additional descriptors to keep open in the child, subject to
            the platform's subprocess support.
        timeout: Positive finite seconds from launch, replacing the client default;
            ``None`` inherits it. Booleans are rejected before launching.
        workload_status: Mark a launch whose exit status belongs to the workload.

    Returns:
        Captured stdout, or ``(stdout, stderr)`` when return_stderr is true.
        Decode with replacement and remove at most one trailing newline from
        each captured stream. Uncaptured streams become empty strings.

    Raises:
        ValueError: If timeout is boolean, nonfinite, zero, or negative; or, inside
            an observation, if output is uncaptured, tty is set, or input is
            supplied without a stdin budget.
        ObservationLimitError: With reason ``total_stdin`` when the input exceeds
            an enclosing stdin budget.
        OSError: If the executable cannot be launched.
        DockerException: If the command exits unsuccessfully, with a subtype
            selected from captured stderr when available.
        DockerTimeout: If the local CLI is still running when its deadline
            expires. Raw command and output attributes remain confidential
            regardless of the automatic diagnostic-text policy.
    """
    validate_timeout(timeout)
    from pywhales._observation import get_observation

    observation = get_observation()
    try:
        policy = get_execution_policy(args)
        subprocess_env = resolve_environment(
            policy.environment, env, frozen=policy.frozen_environment
        )
    except TypeError, ValueError:
        if observation is not None:
            raise ObservationLimitError("environment", None, None) from None
        raise
    timeout = policy.default_timeout if timeout is None else timeout
    validate_timeout(timeout)
    args = [str(x) for x in args]
    subprocess_env = finalize_environment(subprocess_env, args)
    if tty:
        stdout_dest = sys.stdout
    elif capture_stdout:
        stdout_dest = subprocess.PIPE
    else:
        stdout_dest = None
    if tty:
        stderr_dest = sys.stderr
    elif capture_stderr:
        stderr_dest = subprocess.PIPE
    else:
        stderr_dest = None
    _log_command(args, subprocess_env, raw_diagnostics=policy.raw_diagnostics)
    with ExecutionTelemetry(policy.telemetry_observer, "captured") as telemetry:
        if observation is not None:
            if (
                tty
                or not capture_stdout
                or not capture_stderr
                or (input is not None and not observation.stdin_accounts)
            ):
                raise ValueError(
                    "Observation requires captured output, no tty, and input only"
                    " under a stdin budget"
                )
            from pywhales._bounded_capture import bounded_capture

            completed_process = bounded_capture(
                args,
                subprocess_env,
                pass_fds,
                timeout,
                policy,
                observation,
                telemetry=telemetry,
                input=input,
            )
        elif timeout is None and input is None:
            telemetry.start()
            completed_process = subprocess.run(
                args,
                input=input,
                stdout=stdout_dest,
                stderr=stderr_dest,
                env=subprocess_env,
                pass_fds=pass_fds,
            )
        else:
            telemetry.start()
            deadline = None if timeout is None else monotonic() + timeout
            process = Popen(
                args,
                stdin=PIPE if input is not None else None,
                stdout=stdout_dest,
                stderr=stderr_dest,
                env=subprocess_env,
                pass_fds=pass_fds,
            )
            try:
                with process, StdinPump(process, input, telemetry):
                    while True:
                        try:
                            # Bound OS waits without shortening the invocation's deadline.
                            stdout, stderr = process.communicate(
                                timeout=(
                                    None
                                    if deadline is None
                                    else min(86400, max(0, deadline - monotonic()))
                                ),
                            )
                        except subprocess.TimeoutExpired:
                            assert deadline is not None
                            if monotonic() < deadline:
                                continue
                            timed_out = process.poll() is None
                            if timed_out:
                                process.kill()
                            try:
                                stdout, stderr = process.communicate(timeout=1)
                            except subprocess.TimeoutExpired as captured:
                                stdout, stderr = captured.output, captured.stderr
                            process.wait()
                            if timed_out:
                                telemetry.captured(stdout, stderr)
                                raise DockerTimeout(
                                    args,
                                    process.returncode,
                                    stdout,
                                    stderr,
                                    raw_diagnostics=policy.raw_diagnostics,
                                ) from None
                        break
                    telemetry.captured(stdout, stderr)
                    completed_process = subprocess.CompletedProcess(
                        args, process.returncode, stdout, stderr
                    )
            finally:
                telemetry.return_code = process.returncode

        telemetry.return_code = completed_process.returncode
        telemetry.captured(completed_process.stdout, completed_process.stderr)
        if completed_process.returncode != 0:
            exception_type = (
                DockerException
                if workload_status and policy.strict_workload_errors
                else get_docker_exception_type(completed_process.stderr)
            )
            raise exception_type(
                args,
                completed_process.returncode,
                completed_process.stdout,
                completed_process.stderr,
                raw_diagnostics=policy.raw_diagnostics,
            )

    if return_stderr:
        return (
            post_process_stream(completed_process.stdout),
            post_process_stream(completed_process.stderr),
        )
    else:
        return post_process_stream(completed_process.stdout)


def post_process_stream(stream: bytes | None) -> str:
    """Decode captured bytes and remove at most one trailing newline.

    Args:
        stream: Captured payload; ``None`` represents uncaptured output.

    Returns:
        Text decoded with replacement for invalid bytes, or an empty string
        when no captured payload is supplied.
    """
    if stream is None:
        return ""
    return stream.decode(errors="replace").removesuffix("\n")


ValidPath = str | Path
ValidPortMapping = (
    tuple[str | int] | tuple[str | int, str | int] | tuple[str | int, str | int, str]
)


@overload
def to_list(x: str) -> list[str]: ...


@overload
def to_list(x: bytes) -> list[bytes]: ...


@overload
def to_list[T](x: Iterable[T]) -> list[T]: ...


@overload
def to_list[T](x: T) -> list[T]: ...


def to_list(x: object) -> list[object]:
    """Normalize a scalar or iterable to a new list.

    Args:
        x: Input value. Strings and bytes remain single items; other iterables
            are consumed. Scalars, including ``None``, become single-item lists.

    Returns:
        A newly created list in input iteration order, or containing the scalar.
    """
    if isinstance(x, str | bytes):
        return [x]
    if isinstance(x, Iterable):
        return list(x)
    return [x]


class MappedProcessStream[T](Iterator[T]):
    """Output adapter preserving ownership of an already-running process stream.

    Mapping begins on iteration, not construction. Exhaustion closes the owned
    stream and checks recorded terminal failures; mapper errors also trigger
    cleanup. Use a context manager or close() when stopping early, including
    before the first iteration. A bare break does not close the adapter.
    Cleanup affects only the local CLI, not daemon-managed resources.
    """

    def __init__(self, stream: ProcessStream, iterator: Iterator[T]) -> None:
        """Retain a process stream and the iterator transforming its output.

        Args:
            stream: Already-running stream whose cleanup this adapter owns.
            iterator: Lazy output transformation consumed by this adapter.
        """
        self._stream = stream
        self._iterator = iterator

    def _on_failure(
        self, callback: Callable[[OSError | BufferError | DockerException], None]
    ) -> None:
        """Register an owner callback on the underlying process stream.

        Args:
            callback: Nonblocking failure notification, including replay of an
                already recorded process failure.
        """
        self._stream._on_failure(callback)

    @override
    def __iter__(self) -> Self:
        """Return this mapped, process-owning iterator."""
        return self

    @override
    def __next__(self) -> T:
        """Return the next transformed value and clean up on termination.

        Returns:
            The next value produced by the mapping iterator.

        Raises:
            StopIteration: After mapping exhaustion and underlying cleanup.
            DockerException: If the owned stream delivers a process failure.
            DockerTimeout: If the owned stream's launch deadline expires.
            BufferError: If the owned stream exceeds a retained-output bound.
        """
        yielded = False
        try:
            result = next(self._iterator)
            yielded = True
            return result
        except StopIteration:
            self._stream.__exit__(None, None, None)
            raise
        finally:
            if not yielded:
                self.close()

    def close(self) -> None:
        """Close the local process stream and discard the mapping iterator.

        Work even before mapping starts and wait for owned process cleanup.
        Repeated calls are safe; recorded process failures remain available to
        a subsequent clean context exit rather than being raised here.
        """
        self._stream.close()
        self._iterator = iter(())

    def __enter__(self) -> Self:
        """Return this adapter without starting iteration or another process."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the process and discard mapping state on context exit.

        Deliver recorded process errors only when no body exception is active.

        Args:
            exc_type: Active context-body exception type, or ``None``.
            exc_value: Active body exception passed through to stream cleanup.
            traceback: Context-body traceback supplied by the context protocol.
        """
        try:
            self._stream.__exit__(exc_type, exc_value, traceback)
        finally:
            self._iterator = iter(())


def _start_process(
    full_cmd: Sequence[object],
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    pipe_stdin: bool = False,
    *,
    timeout: float | None = None,
    buffer_bytes: int | BufferDefault | None = DEFAULT_BUFFER,
    workload_status: bool = False,
) -> ProcessStream:
    """Launch a process immediately with owned readers and a deadline watchdog.

    Args:
        full_cmd: Executable and arguments, retaining any originating policy
            carrier until its settings have been snapshotted.
        env: Overrides applied after inherited and client environment layers;
            ``None`` adds no operation overrides.
        pass_fds: Additional descriptors to preserve in the child.
        pipe_stdin: Open a writable stdin pipe when true; otherwise inherit stdin.
        timeout: Positive finite seconds from launch, replacing the client default;
            ``None`` inherits it.
        buffer_bytes: Positive byte budget. Omission inherits the current client
            budget; explicit ``None`` is unbounded even if the client has a limit.
        workload_status: Mark a launch whose exit status belongs to the workload.

    Returns:
        An already-running owned stream of ``(source, bytes)`` records. Its
        deadline advances independently of iteration; early consumers must
        close it or use its context manager.

    Inside an active observation the launch first checks cancellation and the
    argv and environment limits, as a captured run does, and the stream keeps
    that scope's deadline and tokens after the scope exits.

    Raises:
        ValueError: If timeout or the selected byte budget is invalid, including
            boolean values. Validation occurs before process launch.
        OSError: If the executable cannot be launched.
        ObservationCancelled: If an active observation was cancelled.
        ObservationTimeout: If an active observation deadline has passed.
        ObservationLimitError: If an active observation rejects the argv or
            environment, before any process starts.
    """
    validate_timeout(timeout)
    from pywhales._observation import get_observation
    from pywhales._scoped_launch import (
        check_scoped_launch,
        environment_limit,
        native_argv_limit,
    )

    observation = get_observation()
    with environment_limit(observation is not None):
        policy = get_execution_policy(full_cmd)
    timeout = policy.default_timeout if timeout is None else timeout
    validate_timeout(timeout)
    if buffer_bytes is DEFAULT_BUFFER:
        buffer_bytes = policy.stream_buffer_bytes
    if buffer_bytes is not None and (
        type(buffer_bytes) is not int or buffer_bytes <= 0
    ):
        raise ValueError("buffer_bytes must be a positive integer or None")
    with environment_limit(observation is not None):
        subprocess_env = resolve_environment(
            policy.environment, env, frozen=policy.frozen_environment
        )

    telemetry = ExecutionTelemetry(policy.telemetry_observer, "stream")
    args: list[str] | None = None
    if observation is not None:
        # Like a captured run, reject before telemetry sees a launch or a
        # process exists: a stopped scope or an oversized argv/environment.
        args = [str(x) for x in full_cmd]
        subprocess_env = finalize_environment(subprocess_env, args)
        check_scoped_launch(args, subprocess_env, observation)
    telemetry.start()
    deadline = None if timeout is None else monotonic() + timeout
    try:
        if args is None:
            args = [str(x) for x in full_cmd]
            subprocess_env = finalize_environment(subprocess_env, args)
        _log_command(
            args,
            subprocess_env,
            raw_diagnostics=policy.raw_diagnostics,
        )
        with nullcontext() if observation is None else native_argv_limit(args):
            process = Popen(
                args,
                stdin=PIPE if pipe_stdin else None,
                stdout=PIPE,
                stderr=PIPE,
                env=subprocess_env,
                pass_fds=pass_fds,
            )
        return ProcessStream(
            process,
            args,
            deadline=deadline,
            buffer_bytes=buffer_bytes,
            raw_diagnostics=policy.raw_diagnostics,
            strict_workload_errors=workload_status and policy.strict_workload_errors,
            telemetry=telemetry if policy.telemetry_observer is not None else None,
            observation=observation,
        )
    except BaseException as error:
        telemetry.finish(outcome_of(type(error)))
        raise


def stream_stdout_and_stderr(
    full_cmd: Sequence[object],
    env: Mapping[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    pipe_stdin: bool = False,
    *,
    timeout: float | None = None,
    buffer_bytes: int | BufferDefault | None = DEFAULT_BUFFER,
    workload_status: bool = False,
) -> ProcessStream:
    """Launch a subprocess and return its owned stdout/stderr record stream.

    Start work at this call, not on iteration or context entry. Iteration blocks
    for records and terminal status. Deadlines continue while consumption pauses
    or never begins. Exhaustion reaps the child; early exit requires a context
    manager or close(), because a bare break does not clean up. Closure or
    timeout kills only the local CLI, not daemon-managed resources.

    Started inside ``observation()``, the launch checks cancellation and the
    argv and environment limits before ``Popen``, and the stream keeps that
    scope's deadline and cancel tokens after the ``with`` block exits. When the
    scope ends first, the local CLI is killed and reaped and iteration raises
    ``ObservationTimeout`` or ``ObservationCancelled``; a per-call ``timeout``
    that is strictly earlier than the scope deadline still raises
    ``DockerTimeout``. Response-byte accounting does not cover stream payloads,
    but the scope's ``max_total_stream_bytes`` does: stdout and stderr bytes are
    charged as the pipe readers pull them, and the first read that overflows any
    enclosing account kills and reaps the local CLI, then raises
    ``ObservationLimitError`` with reason ``total_stream`` once.

    Args:
        full_cmd: Executable and arguments; an originating policy carrier is
            read before converting arguments to strings.
        env: Operation overrides, or ``None`` for inherited and client layers only.
        pass_fds: Additional descriptors kept open in the child, subject to
            platform support.
        pipe_stdin: Make result.stdin writable when true; otherwise it is None
            and the child inherits stdin.
        timeout: Positive finite seconds from launch, replacing the client default;
            ``None`` inherits it.
        buffer_bytes: Positive byte budget; omitted means inherit the client's
            budget, while explicit ``None`` means unbounded. Queue payload,
            unfinished records, and stderr history have separate limits rather
            than one total-memory cap. Producers wait for queue capacity without
            dropping output; oversized records or stderr history fail iteration.
        workload_status: Mark a launch whose exit status belongs to the workload.

    Returns:
        An owned ProcessStream yielding ``(source, payload)`` pairs labelled
        ``stdout`` or ``stderr``. Payloads retain newlines and may include an
        unterminated final record. Terminal failures are delivered through
        iteration or, when already recorded, clean context exit.

    Raises:
        ValueError: Before launch if timeout or the selected budget is invalid;
            booleans are rejected.
        OSError: If process launch fails. Later read errors, BufferError,
            DockerException, DockerTimeout, ObservationTimeout,
            ObservationCancelled, and a ``total_stream`` ObservationLimitError
            are delivered by the stream.
        ObservationCancelled: Before launch if an active observation was cancelled.
        ObservationTimeout: Before launch if an active observation has expired.
        ObservationLimitError: Before launch if an active observation rejects the
            argv or environment size.
    """
    return _start_process(
        full_cmd,
        env,
        pass_fds,
        pipe_stdin,
        timeout=timeout,
        buffer_bytes=buffer_bytes,
        workload_status=workload_status,
    )


def format_mapping_for_cli(
    mapping: Mapping[str, object], separator: str = "="
) -> list[str]:
    """Format mapping entries as CLI values in mapping iteration order.

    Args:
        mapping: Keys and values to stringify without shell quoting or escaping.
        separator: Text joining each key to its value; defaults to ``=``.

    Returns:
        One formatted string per mapping entry.
    """
    return [f"{key}{separator}{value}" for key, value in mapping.items()]


def format_filters_for_cli(
    filters: Mapping[str, object] | Iterable[tuple[str, str]],
) -> list[str]:
    """Format Docker filters from a mapping or an iterable of pairs.

    A mapping is formatted exactly as `format_mapping_for_cli` does, keeping its
    existing output. Any other iterable is read exactly once into pairs that keep their
    order and duplicate keys; nothing is deduplicated or reordered.

    Args:
        filters: Either a mapping of filter names to values, or an iterable of
            ``(key, value)`` tuples whose items are both strings.

    Returns:
        One ``key=value`` string per filter, in input order.

    Raises:
        TypeError: If filters is a string or bytes-like value, or if a pair is
            not a two-item tuple of strings. Raised before any command is built.
    """
    if isinstance(filters, Mapping):
        return [f"{key}={value}" for key, value in filters.items()]
    if isinstance(filters, str | bytes | bytearray):
        raise TypeError("filters must be a mapping or an iterable of pairs")
    pairs = list(filters)
    for position, pair in enumerate(pairs):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError(
                f"filter {position} must be a (key, value) tuple of two strings"
            )
        if not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise TypeError(f"filter {position} must have a string key and value")
    return [f"{key}={value}" for key, value in pairs]


def read_env_file(env_file: Path) -> dict[str, str]:
    """Read simple environment assignments without shell or dotenv expansion.

    Strip each line and everything after its first ``#``, skip blank lines, and
    split remaining assignments at the first ``=``. Quotes and escapes have no
    special meaning; keys and values are not stripped separately after splitting.

    Args:
        env_file: Text file containing assignments; later duplicate keys win.

    Returns:
        Parsed assignment values without quote removal or interpolation.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If a nonblank, noncomment line contains no equals sign.
    """
    result_dict: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        try:
            first_sharp = line.index("#")
        except ValueError:
            pass
        else:
            line = line[:first_sharp]
        if not line:
            continue
        line = line.strip()
        key, value = line.split("=", 1)
        result_dict[key] = value
    return result_dict


def read_env_files(env_files: list[Path]) -> dict[str, str]:
    """Merge simple environment files in the supplied order.

    Args:
        env_files: Files parsed by read_env_file; later assignments override
            earlier values. Empty input produces an empty mapping.

    Returns:
        The combined environment assignments.

    Raises:
        OSError: If any file cannot be read.
        ValueError: If any file contains an assignment without an equals sign.
    """
    result_dict: dict[str, str] = {}
    for file in env_files:
        result_dict.update(read_env_file(file))
    return result_dict


def format_time_arg(time_object: datetime | timedelta | None) -> str | None:
    """Format a Docker time argument while preserving omission.

    Args:
        time_object: Timestamp or duration, or ``None`` to omit the argument.

    Returns:
        An ISO timestamp, duration in seconds with an ``s`` suffix, or ``None``.
    """
    if time_object is None:
        return None
    else:
        return format_time_for_docker(time_object)


def format_time_for_docker(time_object: datetime | timedelta) -> str:
    """Format a timestamp or duration for a Docker CLI time option.

    Args:
        time_object: Timestamp to preserve as ISO text or duration in seconds.

    Returns:
        The timestamp's isoformat result or total seconds followed by ``s``.
    """
    if isinstance(time_object, datetime):
        return time_object.isoformat()
    return f"{time_object.total_seconds()}s"


def format_signal_arg(signal_object: int | str | None) -> str | None:
    """Format a Docker signal argument while preserving omission.

    Args:
        signal_object: Signal number or name, or ``None`` to omit the option.

    Returns:
        The normalized signal spelling, or ``None`` for omitted input.

    Raises:
        ValueError: If a signal number is not recognized on this platform.
    """
    if signal_object is None:
        return None
    else:
        return format_signal_for_docker(signal_object)


def format_signal_for_docker(signal_object: int | str) -> str:
    """Convert a signal number or abbreviated name to Docker CLI text.

    Args:
        signal_object: Platform signal number, abbreviated name such as TERM,
            or text passed through unchanged when no prefixed name exists.

    Returns:
        The platform's signal name, a recognized ``SIG``-prefixed name, or the
        original string. Arbitrary string values are not validated here.

    Raises:
        ValueError: If a numeric signal is not recognized on this platform.
        TypeError: If called with a value that is neither an integer nor text.
    """
    if isinstance(signal_object, int):
        return signal.Signals(signal_object).name
    elif isinstance(signal_object, str):
        if hasattr(signal.Signals, "SIG" + signal_object):
            return "SIG" + signal_object
        else:
            return signal_object
    else:
        raise TypeError(f"Got unexpected signal type {type(signal_object).__name__!r}")


def format_port_arg(port_object: ValidPortMapping) -> str:
    """Format a one-, two-, or three-part published-port tuple.

    Args:
        port_object: Container port alone, host and container ports, or host and
            container ports followed by the protocol spelling.

    Returns:
        Container-port text, ``host:container``, or ``host:container/protocol``.

    Raises:
        ValueError: If the tuple contains fewer than one or more than three items.
    """
    if len(port_object) == 1:
        return str(port_object[0])
    elif len(port_object) == 2:
        return f"{port_object[0]}:{port_object[1]}"
    elif len(port_object) == 3:
        return f"{port_object[0]}:{port_object[1]}/{port_object[2]}"
    else:
        raise ValueError(
            "The size of the tuples in the publish list must be 1, 2, or 3"
        )


def parse_ls_status_count(status_output: str, status: str) -> int:
    """Extract the first count following a named status in CLI summary text.

    Args:
        status_output: Summary containing a fragment such as ``Running(2)``.
        status: Exact, case-sensitive status label preceding the parenthesis.

    Returns:
        The parsed count, or zero when the status fragment is absent.

    Raises:
        ValueError: If the located count cannot be parsed as an integer.
    """
    try:
        return int(status_output.split(status + "(")[1].split(")")[0])
    except IndexError:
        return 0


def join_if_not_none(sequence: Sequence[object] | None) -> str | None:
    """Join stringified values with commas while preserving omitted input.

    Args:
        sequence: Values to join in order, or ``None`` to omit the argument.

    Returns:
        Comma-separated text, an empty string for an empty sequence, or ``None``.
    """
    if sequence is None:
        return None
    return ",".join(str(x) for x in sequence)


def format_cpuset(value: Iterable[int] | str | None) -> str | None:
    """Format CPU indices while preserving range text and omitted input.

    Args:
        value: CPU indices to join, existing CLI range text to retain unchanged,
            or ``None`` to omit the option.

    Returns:
        Comma-separated indices, the original string, or ``None``. An empty
        iterable produces an empty string rather than omitting the option.
    """
    if value is None or isinstance(value, str):
        return value
    return ",".join(str(x) for x in value)


def to_seconds(duration: int | timedelta | None) -> str | None:
    """Format whole seconds with a suffix while preserving omitted input.

    Args:
        duration: Seconds or a timedelta; fractional timedelta seconds truncate
            toward zero. ``None`` omits the option.

    Returns:
        The seconds value followed by ``s``, including zero, or ``None``.
    """
    if duration is None:
        return None
    if isinstance(duration, timedelta):
        duration = int(duration.total_seconds())
    return f"{duration}s"
