"""Closed, immutable application policy inputs, never arbitrary Docker kwargs."""

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, override

from pywhales.admission import EndpointIdentity


class PolicyReason(StrEnum):
    """Payload-free policy failure categories."""

    INVALID = "invalid"
    NOT_APPROVED = "not_approved"
    UNSUPPORTED = "unsupported"
    LIMIT = "limit"
    CONFLICT = "conflict"


@dataclass(slots=True)
class PolicyError(Exception):
    """A field and fixed reason, never a request or secret value."""

    field: str
    reason: PolicyReason

    @override
    def __str__(self) -> str:
        return f"profile rejected {self.field}: {self.reason.value}"


def token(value: str, name: str) -> None:
    """Require a safe metadata token without echoing invalid input."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise PolicyError(name, PolicyReason.INVALID)


def _require_type(
    value: object, expected: type[object] | tuple[type[object], ...], name: str
) -> None:
    if not isinstance(value, expected):
        raise PolicyError(name, PolicyReason.INVALID)


class LogMode(StrEnum):
    """Explicit operator choice between backpressure and possible loss."""

    BLOCKING = "blocking"
    NON_BLOCKING = "non-blocking"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceLimits:
    """Positive CPU count, memory bytes and PID count, also usable as ceilings."""

    cpus: float
    memory_bytes: int
    pids: int

    def __post_init__(self) -> None:
        if (
            type(self.cpus) not in (int, float)
            or not math.isfinite(self.cpus)
            or self.cpus <= 0
        ):
            raise PolicyError("resources.cpus", PolicyReason.INVALID)
        for name, value in (("memory_bytes", self.memory_bytes), ("pids", self.pids)):
            if type(value) is not int or value <= 0:
                raise PolicyError(f"resources.{name}", PolicyReason.INVALID)


@dataclass(frozen=True, slots=True, kw_only=True)
class LogPolicy:
    """Supported rotating driver and explicit delivery/retention policy.

    Non-blocking delivery requires explicit loss approval and buffer bytes.
    Blocking delivery accepts backpressure; neither mode guarantees audit delivery.
    """

    driver: str
    max_size_bytes: int
    max_files: int
    mode: LogMode
    loss_approved: bool
    buffer_bytes: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, LogMode) or type(self.loss_approved) is not bool:
            raise PolicyError("logging.mode", PolicyReason.INVALID)
        if self.driver not in ("json-file", "local"):
            raise PolicyError("logging.driver", PolicyReason.UNSUPPORTED)
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_size_bytes, self.max_files)
        ):
            raise PolicyError("logging.retention", PolicyReason.INVALID)
        match self.mode:
            case LogMode.NON_BLOCKING:
                if (
                    not self.loss_approved
                    or type(self.buffer_bytes) is not int
                    or self.buffer_bytes <= 0
                ):
                    raise PolicyError("logging.loss", PolicyReason.CONFLICT)
            case LogMode.BLOCKING:
                if self.buffer_bytes is not None:
                    raise PolicyError("logging.buffer", PolicyReason.CONFLICT)
            case _:
                raise PolicyError("logging.mode", PolicyReason.INVALID)


@dataclass(frozen=True, slots=True, kw_only=True)
class Mount:
    """Exact approved bind or volume mount, not a raw CLI fragment."""

    kind: str
    source: str = field(repr=False)
    target: str = field(repr=False)
    read_only: bool

    def __post_init__(self) -> None:
        _require_type(self.source, str, "mounts.source")
        _require_type(self.target, str, "mounts.target")
        if type(self.read_only) is not bool:
            raise PolicyError("mounts.read_only", PolicyReason.INVALID)
        if self.kind not in ("bind", "volume"):
            raise PolicyError("mounts.kind", PolicyReason.UNSUPPORTED)
        if (
            not self.source
            or not self.target.startswith("/")
            or any(
                char in value
                for value in (self.source, self.target)
                for char in (",", '"', "\x00", "\n", "\r")
            )
        ):
            raise PolicyError("mounts", PolicyReason.INVALID)


CONTROLS: Final = frozenset(
    {
        "user",
        "capabilities",
        "privilege",
        "read_only",
        "security_options",
        "mounts",
        "networks",
        "cpu",
        "memory",
        "pids",
        "logging",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimePolicy:
    """Operator-declared qualified runtime capabilities, not an automatic probe."""

    name: str
    rootless: bool
    supported_controls: frozenset[str]

    def __post_init__(self) -> None:
        token(self.name, "runtime")
        if type(self.rootless) is not bool:
            raise PolicyError("runtime.rootless", PolicyReason.INVALID)
        object.__setattr__(
            self, "supported_controls", frozenset(self.supported_controls)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateRequest:
    """The complete supported create surface; omitted Docker options cannot bypass it.

    No plaintext environment, env-file, device, raw mount, namespace, label-file,
    or extra-kwargs escape is offered. Secret material must not be put in command.
    Approved external secret files can be referenced through exact mounts.
    """

    endpoint: EndpointIdentity
    image: str = field(repr=False)
    command: tuple[str, ...] = field(repr=False)
    user: str = field(repr=False)
    capabilities: tuple[str, ...]
    privileged: bool
    read_only: bool
    security_options: tuple[str, ...] = field(repr=False)
    mounts: tuple[Mount, ...] = field(repr=False)
    networks: tuple[str, ...] = field(repr=False)
    resources: ResourceLimits
    logging: LogPolicy
    runtime: str
    rootless: bool
    pull: str
    health_start_interval: int | None = None

    def __post_init__(self) -> None:
        if self.health_start_interval is not None and (
            type(self.health_start_interval) is not int
            or self.health_start_interval <= 0
        ):
            raise PolicyError("health_start_interval", PolicyReason.INVALID)
        _require_type(self.endpoint, EndpointIdentity, "endpoint")
        _require_type(self.resources, ResourceLimits, "resources")
        _require_type(self.logging, LogPolicy, "logging")
        for name in (
            "command",
            "capabilities",
            "security_options",
            "mounts",
            "networks",
        ):
            value = getattr(self, name)
            _require_type(value, (tuple, list), name)
            object.__setattr__(self, name, tuple(value))
        for mount in self.mounts:
            _require_type(mount, Mount, "mounts")
        if self.pull not in ("never", "missing", "always"):
            raise PolicyError("pull", PolicyReason.INVALID)
        for name, value in (
            ("privileged", self.privileged),
            ("read_only", self.read_only),
            ("rootless", self.rootless),
        ):
            if type(value) is not bool:
                raise PolicyError(name, PolicyReason.INVALID)
        for name, values in (
            ("image", (self.image,)),
            ("user", (self.user,)),
            ("runtime", (self.runtime,)),
            ("command", self.command),
            ("capabilities", self.capabilities),
            ("security_options", self.security_options),
            ("networks", self.networks),
        ):
            for value in values:
                _require_type(value, str, name)
                if "\x00" in value:
                    raise PolicyError(name, PolicyReason.INVALID)
        if not self.image or not self.user or not self.runtime:
            raise PolicyError("request", PolicyReason.INVALID)
