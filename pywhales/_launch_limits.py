"""Check bounded command launches against explicit and native size limits."""

import os
import struct
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from pywhales.exceptions import ObservationLimitError

if TYPE_CHECKING:
    from pywhales.inventory import InspectionPolicy

_POINTER_SIZE = struct.calcsize("P")


def argv_size(args: Sequence[str]) -> int:
    """Return serialized argv bytes including terminators on the current platform."""
    if any("\0" in arg for arg in args):
        raise ObservationLimitError("nul", None, None)
    if os.name == "nt":
        return (
            len(
                subprocess.list2cmdline(args).encode(
                    "utf-16-le", errors="surrogatepass"
                )
            )
            + 2
        )
    return sum(len(os.fsencode(arg)) + 1 for arg in args)


def check_launch(
    args: Sequence[str], env: Mapping[str, str], policy: InspectionPolicy
) -> None:
    """Check complete argv and the exact environment passed to the subprocess.

    Args:
        args: Complete command, including executable and global options.
        env: Effective environment, including inherited and runner-added entries.
        policy: Explicit caller limits.

    Raises:
        ObservationLimitError: If a limit is exceeded, unknown, or a NUL is present.
    """
    size = argv_size(args)
    if size > policy.max_argv_bytes:
        raise ObservationLimitError("argv", policy.max_argv_bytes, size)
    if any("\0" in key or "\0" in value or "=" in key for key, value in env.items()):
        raise ObservationLimitError("environment", None, None)
    if os.name == "nt":
        command_units = size // 2
        if command_units > 32767:
            raise ObservationLimitError("native_argv", 32767, command_units)
        environment_units = (
            sum(
                len(f"{key}={value}".encode("utf-16-le", errors="surrogatepass")) // 2
                + 1
                for key, value in env.items()
            )
            + 1
        )
        if environment_units > 32767:
            raise ObservationLimitError("native_environment", 32767, environment_units)
        return
    try:
        native_limit = os.sysconf("SC_ARG_MAX")
        page_size = os.sysconf("SC_PAGESIZE") if sys.platform.startswith("linux") else 1
    except OSError, ValueError:
        raise ObservationLimitError("native_limit_unknown", None, None) from None
    if native_limit <= 0 or page_size <= 0:
        raise ObservationLimitError("native_limit_unknown", None, None)
    strings = [os.fsencode(arg) for arg in args]
    strings.extend(os.fsencode(f"{key}={value}") for key, value in env.items())
    if sys.platform.startswith("linux"):
        string_limit = 32 * page_size
        for value in strings:
            measured = len(value) + 1
            if measured > string_limit:
                raise ObservationLimitError("native_string", string_limit, measured)
    total = sum(len(value) + 1 for value in strings)
    total += (len(args) + len(env) + 2) * struct.calcsize("P")
    if total > native_limit:
        raise ObservationLimitError("native_total", native_limit, total)


@dataclass(frozen=True, slots=True)
class _Argument:
    """One argv string measured once for the planner's platform.

    ``size`` is the string's share of the serialized argv: its filesystem-encoded
    bytes plus the NUL terminator on POSIX, or its quoted UTF-16 bytes plus one
    separator or terminator on Windows. ``value`` is kept to re-raise an encoding
    failure exactly as ``argv_size`` would.
    """

    value: str
    size: int
    nul: bool
    encodable: bool


class LaunchPlanner:
    """Share one platform reading and one native-limit reading across a plan.

    Build one planner per planning call and derive a measurement for each command
    prefix and environment. The platform is fixed at construction. ``os.sysconf`` is
    read at most once, when a measurement first reaches the POSIX native checks, and
    is never shared with another planner, so each planning call sees current limits.
    """

    __slots__ = ("_limits", "_limits_read", "_linux", "_policy", "_windows")

    def __init__(self, policy: InspectionPolicy) -> None:
        """Fix the explicit limits and platform for this planning call.

        Args:
            policy: Explicit caller limits applied by every measurement.
        """
        self._policy = policy
        self._windows = os.name == "nt"
        self._linux = sys.platform.startswith("linux")
        self._limits: tuple[int, int] | None = None
        self._limits_read = False

    def measure(
        self, prefix: Sequence[str], env: Mapping[str, str]
    ) -> LaunchMeasurement:
        """Return a measurement of one fixed argv prefix and its environment.

        Args:
            prefix: Leading arguments shared by every planned launch, including the
                executable and global options.
            env: Exact environment each planned launch passes to the subprocess,
                such as the snapshot later frozen with ``frozen_environment_scope``.

        Returns:
            A measurement with no appended arguments. It reads ``env`` once now.
        """
        return LaunchMeasurement(self, prefix, env)

    def _argument(self, value: str) -> _Argument:
        nul = "\0" in value
        if self._windows:
            quoted = subprocess.list2cmdline([value]).encode(
                "utf-16-le", errors="surrogatepass"
            )
            return _Argument(value, len(quoted) + 2, nul, True)
        try:
            return _Argument(value, len(os.fsencode(value)) + 1, nul, True)
        except UnicodeEncodeError:
            return _Argument(value, 0, nul, False)

    def _native_limits(self) -> tuple[int, int]:
        """Return ``SC_ARG_MAX`` and the Linux page size, reading them once.

        Raises:
            ObservationLimitError: If either limit is unavailable or not positive.
        """
        if not self._limits_read:
            try:
                native_limit = os.sysconf("SC_ARG_MAX")
                page_size = os.sysconf("SC_PAGESIZE") if self._linux else 1
            except OSError, ValueError:
                limits = None
            else:
                positive = native_limit > 0 and page_size > 0
                limits = (native_limit, page_size) if positive else None
            self._limits, self._limits_read = limits, True
        if self._limits is None:
            raise ObservationLimitError("native_limit_unknown", None, None)
        return self._limits


class LaunchMeasurement:
    """Running size of one launch: a fixed prefix plus appended arguments.

    The prefix and environment are measured once and each appended argument once,
    so a check costs constant time outside its refusal paths. A refusal is exactly
    what ``check_launch`` raises for the complete argv and environment: the same
    exception, reason, limit and measured value, found in the same order. Instances
    are not thread-safe; create them with ``LaunchPlanner.measure``.
    """

    __slots__ = (
        "_arguments",
        "_base",
        "_env",
        "_env_sizes",
        "_env_units",
        "_env_valid",
        "_longest",
        "_nul",
        "_planner",
        "_prefix",
        "_size",
        "_unencodable",
    )

    def __init__(
        self, planner: LaunchPlanner, prefix: Sequence[str], env: Mapping[str, str]
    ) -> None:
        """Measure the prefix and snapshot the environment.

        Args:
            planner: Planning call supplying the policy, platform and native limits.
            prefix: Leading arguments shared by every measured launch.
            env: Exact environment each measured launch passes to the subprocess.
        """
        self._planner = planner
        self._prefix = [planner._argument(value) for value in prefix]
        self._arguments: list[_Argument] = []
        self._env_valid = not any(
            "\0" in key or "\0" in value or "=" in key for key, value in env.items()
        )
        self._env = [f"{key}={value}" for key, value in env.items()]
        self._env_units = (
            sum(
                len(entry.encode("utf-16-le", errors="surrogatepass")) // 2 + 1
                for entry in self._env
            )
            + 1
            if planner._windows
            else 0
        )
        self._env_sizes: tuple[int, int, list[int]] | None = None
        self._size = 0
        self._nul = False
        self._unencodable: _Argument | None = None
        self._longest = 0
        for argument in self._prefix:
            self._include(argument)
        self._base = (self._size, self._nul, self._unencodable, self._longest)

    def append(self, argument: str) -> None:
        """Measure one more argument after the prefix and earlier appended ones.

        Args:
            argument: Next argv string. It is encoded now and judged by ``check``.
        """
        self._push(self._planner._argument(argument))

    def reset(self) -> None:
        """Drop appended arguments, keeping the measured prefix and environment."""
        self._arguments.clear()
        self._size, self._nul, self._unencodable, self._longest = self._base

    def check(self) -> None:
        """Raise what ``check_launch`` raises for the prefix and appended arguments.

        Raises:
            ObservationLimitError: If an argument contains a NUL, the argv exceeds
                the policy or a native limit, the environment is invalid or too
                large, or the POSIX native limit is unknown.
            UnicodeEncodeError: If a POSIX argument or environment entry cannot use
                the filesystem encoding.
        """
        planner = self._planner
        policy = planner._policy
        if self._nul:
            raise ObservationLimitError("nul", None, None)
        if self._unencodable is not None:
            # Encoding the same value raises the same error as ``argv_size``.
            os.fsencode(self._unencodable.value)
        count = len(self._prefix) + len(self._arguments)
        size = self._size if count or not planner._windows else 2
        if size > policy.max_argv_bytes:
            raise ObservationLimitError("argv", policy.max_argv_bytes, size)
        if not self._env_valid:
            raise ObservationLimitError("environment", None, None)
        if planner._windows:
            command_units = size // 2
            if command_units > 32767:
                raise ObservationLimitError("native_argv", 32767, command_units)
            if self._env_units > 32767:
                raise ObservationLimitError(
                    "native_environment", 32767, self._env_units
                )
            return
        native_limit, page_size = planner._native_limits()
        env_total, env_longest, env_sizes = self._environment()
        if planner._linux:
            string_limit = 32 * page_size
            if max(self._longest, env_longest) > string_limit:
                measured = next(
                    value
                    for value in chain(
                        (argument.size for argument in self._prefix),
                        (argument.size for argument in self._arguments),
                        env_sizes,
                    )
                    if value > string_limit
                )
                raise ObservationLimitError("native_string", string_limit, measured)
        total = self._size + env_total + (count + len(env_sizes) + 2) * _POINTER_SIZE
        if total > native_limit:
            raise ObservationLimitError("native_total", native_limit, total)

    def chunks(self, arguments: Sequence[str], max_count: int) -> Iterator[slice]:
        """Split arguments into maximal launchable runs after the prefix.

        Each run starts where the previous one ended and is the longest that holds
        at most ``max_count`` arguments and passes ``check``. Every argument is
        measured once. Work happens on iteration: a run is yielded before the next
        argument is checked alone, so an argument that cannot launch even alone
        raises what its single launch would raise, after the runs before it. The
        appended arguments are replaced while iterating.

        Args:
            arguments: Arguments to append after the prefix, in launch order.
            max_count: Largest number of arguments in one run; at least one.

        Yields:
            Consecutive slices of ``arguments`` that cover it in order.

        Raises:
            ObservationLimitError: If an argument cannot launch alone after the
                prefix, with the reason, limit and measured value of that launch.
            UnicodeEncodeError: If a POSIX argument or environment entry cannot use
                the filesystem encoding.
        """
        self.reset()
        start = 0
        for index, value in enumerate(arguments):
            argument = self._planner._argument(value)
            if 0 < index - start < max_count:
                self._push(argument)
                try:
                    self.check()
                except ObservationLimitError, UnicodeEncodeError:
                    pass
                else:
                    continue
            if index > start:
                yield slice(start, index)
                start = index
            self.reset()
            self._push(argument)
            self.check()
        if len(arguments) > start:
            yield slice(start, len(arguments))

    def _include(self, argument: _Argument) -> None:
        self._size += argument.size
        self._nul = self._nul or argument.nul
        if self._unencodable is None and not argument.encodable:
            self._unencodable = argument
        self._longest = max(self._longest, argument.size)

    def _push(self, argument: _Argument) -> None:
        self._arguments.append(argument)
        self._include(argument)

    def _environment(self) -> tuple[int, int, list[int]]:
        """Return POSIX environment total, longest and per-entry sizes, encoding once.

        Raises:
            UnicodeEncodeError: If an entry cannot use the filesystem encoding; the
                next call encodes again, as ``check_launch`` would.
        """
        if self._env_sizes is None:
            sizes = [len(os.fsencode(entry)) + 1 for entry in self._env]
            self._env_sizes = (sum(sizes), max(sizes, default=0), sizes)
        return self._env_sizes
