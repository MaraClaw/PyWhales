"""Originating-config carriers and per-launch execution policy snapshots.

Resolve policy before converting command arguments to plain strings so later
config mutations affect new launches without altering an existing snapshot.
"""

from collections.abc import Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum, auto
from types import MappingProxyType
from typing import Final, Protocol, Self

from pywhales._environment import (
    EnvironmentFailure,
    consume_frozen_environment,
    copy_environment,
    resolve_environment,
)
from pywhales.telemetry import TelemetryObserver


class ExecutionPolicySource(Protocol):
    """Readable client settings used to snapshot each subprocess launch."""

    @property
    def environment(self) -> Mapping[str, str] | None:
        """Return the current client environment overlay."""
        ...

    @property
    def inherit_environment(self) -> bool:
        """Return whether launches inherit the complete parent environment."""
        ...

    @property
    def inherited_environment_names(self) -> Collection[str] | None:
        """Return exact ambient names, or None for legacy inheritance selection."""
        ...

    @property
    def stream_buffer_bytes(self) -> int | None:
        """Return the current byte budget, or None for unbounded streams."""
        ...

    @property
    def raw_diagnostics(self) -> bool:
        """Return whether new launches may expose raw diagnostic values."""
        ...

    @property
    def strict_workload_errors(self) -> bool:
        """Return whether workload failures avoid stderr classification."""
        ...

    @property
    def strict_env_transport(self) -> bool:
        """Return whether assignment values must avoid literal argv fallback."""
        ...

    @property
    def telemetry_observer(self) -> TelemetryObserver | None:
        """Return the optional sanitized execution observer for new launches."""
        ...

    @property
    def default_timeout(self) -> float | None:
        """Return the default local CLI deadline in seconds."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Immutable stream and diagnostic settings for one subprocess launch.

    Attributes:
        stream_buffer_bytes: Retained-output budget in bytes; ``None`` is
            unbounded. Queue payload, pending records, and stderr history have
            separate limits rather than a single total-memory cap.
        raw_diagnostics: Whether automatic diagnostics may include confidential
            command, environment, and captured-output values.
        telemetry_observer: Optional payload-free observer snapshotted by reference.
        environment: Immutable client overlay captured at policy selection.
        frozen_environment: Preselected full environment or its validation failure;
            a bound container environment supersedes either during resolution.
        default_timeout: Default local CLI deadline in seconds, or None.
        strict_workload_errors: Whether marked workload failures remain generic.
    """

    stream_buffer_bytes: int | None = None
    raw_diagnostics: bool = False
    telemetry_observer: TelemetryObserver | None = None
    environment: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    frozen_environment: Mapping[str, str] | EnvironmentFailure | None = field(
        default=None, repr=False
    )
    default_timeout: float | None = None
    strict_workload_errors: bool = False


class BufferDefault(Enum):
    """Sentinel distinguishing client-inherited budgets from explicit None."""

    CLIENT = auto()


DEFAULT_BUFFER: Final = BufferDefault.CLIENT

_timeout_scopes: ContextVar[tuple[tuple[ExecutionPolicySource, float], ...]] = (
    ContextVar("pywhales_execution_timeouts", default=())
)


@contextmanager
def execution_timeout(
    source: ExecutionPolicySource, timeout: float | None
) -> Generator[None]:
    """Bound implicit launches of one config without mutating it.

    Policy snapshots taken inside the scope use ``timeout`` as their default
    deadline when ``source`` is the identical config; an explicit launch timeout
    still wins. The innermost matching scope applies, and ``None`` binds nothing.
    The caller validates ``timeout``.

    Args:
        source: Originating config, matched by identity rather than equality.
        timeout: Positive finite seconds, or None to leave the policy untouched.
    """
    if timeout is None:
        yield
        return
    token = _timeout_scopes.set((*_timeout_scopes.get(), (source, timeout)))
    try:
        yield
    finally:
        _timeout_scopes.reset(token)


def _scoped_timeout(source: ExecutionPolicySource) -> float | None:
    for scoped, timeout in reversed(_timeout_scopes.get()):
        if scoped is source:
            return timeout
    return source.default_timeout


class PolicyExecutable(str):
    """Executable text retaining its originating, mutable policy source.

    Shallow and deep copies preserve this carrier and its source rather than
    copying the config. The policy is read later, at subprocess launch.

    Attributes:
        policy_source: Originating config used for subsequent policy snapshots.
    """

    policy_source: ExecutionPolicySource

    def __new__(cls, value: str, policy_source: ExecutionPolicySource) -> Self:
        """Create executable text linked to a policy source.

        Args:
            value: Executable path to retain as the string value.
            policy_source: Live settings source retained by reference.

        Returns:
            The executable carrier; no policy snapshot is taken yet.
        """
        executable = super().__new__(cls, value)
        executable.policy_source = policy_source
        return executable

    def __copy__(self) -> Self:
        """Return this carrier without copying its policy source."""
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        """Return this carrier while preserving the originating source.

        Args:
            memo: Deep-copy bookkeeping, unused because the carrier is shared.

        Returns:
            This same executable carrier.
        """
        return self


def get_execution_policy(command: Sequence[object]) -> ExecutionPolicy:
    """Snapshot the first argument's policy source before stringifying argv.

    Args:
        command: Command arguments whose first item may be a PolicyExecutable.

    Returns:
        Current source settings, retaining environment validation failures for
        resolution after any bound container snapshot is applied. Without a policy
        carrier, use safe diagnostics and an unbounded stream budget.
    """
    if command and isinstance(command[0], PolicyExecutable):
        source = command[0].policy_source
        frozen: Mapping[str, str] | EnvironmentFailure | None = (
            consume_frozen_environment(source)
        )
        environment: Mapping[str, str] = MappingProxyType({})
        try:
            current = source.environment if frozen is None else None
            if frozen is None and (
                not source.inherit_environment
                or source.inherited_environment_names is not None
            ):
                frozen = MappingProxyType(
                    resolve_environment(
                        current,
                        inherit=source.inherit_environment,
                        inherited_names=source.inherited_environment_names,
                    )
                )
            elif current is not None:
                environment = MappingProxyType(copy_environment(current))
        except (TypeError, ValueError) as error:
            frozen = EnvironmentFailure(error)
        return ExecutionPolicy(
            source.stream_buffer_bytes,
            source.raw_diagnostics,
            source.telemetry_observer,
            environment,
            frozen,
            _scoped_timeout(source),
            source.strict_workload_errors,
        )
    return ExecutionPolicy()
