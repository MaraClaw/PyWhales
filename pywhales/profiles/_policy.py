"""Pure validation and sanitized planning for explicitly selected profiles."""

import re
from dataclasses import dataclass, field

from pywhales.docker_client import DockerClient

from ._binding import EndpointBinding
from ._contracts import (
    CONTROLS,
    CreateRequest,
    LogPolicy,
    Mount,
    PolicyError,
    PolicyReason,
    ResourceLimits,
    RuntimePolicy,
    token,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityProfile:
    """Named versioned operator policy; no registry or isolation defaults.

    Mounts and security options are exact allowlists, not prefix matching. Only
    references to approved secret files belong here, never their contents.
    """

    name: str
    version: str
    bindings: tuple[EndpointBinding, ...] = field(repr=False)
    registries: frozenset[str]
    require_digest: bool
    users: frozenset[str] = field(repr=False)
    capabilities: frozenset[str]
    allow_privileged: bool
    require_read_only: bool
    security_options: tuple[str, ...] = field(repr=False)
    mounts: frozenset[Mount] = field(repr=False)
    networks: frozenset[str] = field(repr=False)
    ceilings: ResourceLimits
    logging: LogPolicy
    runtimes: tuple[RuntimePolicy, ...]
    max_health_start_interval: int | None = None

    def __post_init__(self) -> None:
        if self.max_health_start_interval is not None and (
            type(self.max_health_start_interval) is not int
            or self.max_health_start_interval <= 0
        ):
            raise PolicyError("profile.max_health_start_interval", PolicyReason.INVALID)
        token(self.name, "profile.name")
        token(self.version, "profile.version")
        for name in ("bindings", "security_options", "runtimes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("registries", "users", "capabilities", "mounts", "networks"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        if (
            not self.bindings
            or not self.registries
            or not self.users
            or not self.runtimes
        ):
            raise PolicyError("profile", PolicyReason.INVALID)
        runtime_keys = {(runtime.name, runtime.rootless) for runtime in self.runtimes}
        if len(runtime_keys) != len(self.runtimes):
            raise PolicyError("profile.runtimes", PolicyReason.CONFLICT)


@dataclass(frozen=True, slots=True)
class PlanSummary:
    """Payload-free display surface; approved execution inputs stay private."""

    profile: str
    version: str
    endpoint: str
    approved: bool = True


@dataclass(frozen=True, slots=True)
class CreatePlan:
    """Validated inputs retained confidentially; creation revalidates the binding."""

    profile: SecurityProfile = field(repr=False)
    request: CreateRequest = field(repr=False)

    @property
    def summary(self) -> PlanSummary:
        """Return only safe profile/version/endpoint metadata."""
        return PlanSummary(
            self.profile.name, self.profile.version, self.request.endpoint.alias
        )


def plan_create(
    profile: SecurityProfile, request: CreateRequest, client: DockerClient
) -> CreatePlan:
    """Validate the complete supported request without CLI lookup or Docker I/O.

    Raises:
        PolicyError: A field is invalid, unapproved, conflicting or unsupported.
            The error never contains the rejected value or complete request.
    """
    bindings = [
        binding
        for binding in profile.bindings
        if binding.endpoint == request.endpoint
        and binding.config is client.client_config
    ]
    if not bindings:
        raise PolicyError("endpoint", PolicyReason.NOT_APPROVED)
    bindings[0].validate(client)
    image = request.image
    registry, separator, repository = image.partition("/")
    if (
        not separator
        or not repository
        or registry not in profile.registries
        or not ("." in registry or ":" in registry or registry == "localhost")
    ):
        raise PolicyError("image.registry", PolicyReason.NOT_APPROVED)
    if image.startswith("-") or any(char.isspace() or char == "\x00" for char in image):
        raise PolicyError("image", PolicyReason.INVALID)
    if (
        profile.require_digest
        and re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image) is None
    ):
        raise PolicyError("image.digest", PolicyReason.NOT_APPROVED)
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    if (
        re.fullmatch(
            rf"{component}(?:/{component})*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}})?(?:@sha256:[0-9a-f]{{64}})?",
            repository,
        )
        is None
    ):
        raise PolicyError("image", PolicyReason.INVALID)
    if request.user not in profile.users:
        raise PolicyError("user", PolicyReason.NOT_APPROVED)
    if not set(request.capabilities) <= profile.capabilities:
        raise PolicyError("capabilities", PolicyReason.NOT_APPROVED)
    if request.privileged and not profile.allow_privileged:
        raise PolicyError("privileged", PolicyReason.NOT_APPROVED)
    if profile.require_read_only and not request.read_only:
        raise PolicyError("read_only", PolicyReason.NOT_APPROVED)
    if request.security_options != profile.security_options:
        raise PolicyError("security_options", PolicyReason.NOT_APPROVED)
    if not set(request.mounts) <= profile.mounts or len(
        {mount.target for mount in request.mounts}
    ) != len(request.mounts):
        raise PolicyError("mounts", PolicyReason.NOT_APPROVED)
    if not request.networks or not set(request.networks) <= profile.networks:
        raise PolicyError("networks", PolicyReason.NOT_APPROVED)
    limits, ceilings = request.resources, profile.ceilings
    if (
        limits.cpus > ceilings.cpus
        or limits.memory_bytes > ceilings.memory_bytes
        or limits.pids > ceilings.pids
    ):
        raise PolicyError("resources", PolicyReason.LIMIT)
    if request.logging != profile.logging:
        raise PolicyError("logging", PolicyReason.NOT_APPROVED)
    interval = request.health_start_interval
    ceiling = profile.max_health_start_interval
    if ceiling is not None and (type(ceiling) is not int or ceiling <= 0):
        raise PolicyError("profile.max_health_start_interval", PolicyReason.INVALID)
    if interval is not None:
        if type(interval) is not int or interval <= 0:
            raise PolicyError("health_start_interval", PolicyReason.INVALID)
        if ceiling is None:
            raise PolicyError("health_start_interval", PolicyReason.NOT_APPROVED)
        if interval > ceiling:
            raise PolicyError("health_start_interval", PolicyReason.LIMIT)
    runtimes = [
        runtime
        for runtime in profile.runtimes
        if (runtime.name, runtime.rootless) == (request.runtime, request.rootless)
    ]
    if not runtimes or not runtimes[0].supported_controls >= CONTROLS:
        raise PolicyError("runtime", PolicyReason.UNSUPPORTED)
    if (
        interval is not None
        and "health_start_interval" not in runtimes[0].supported_controls
    ):
        raise PolicyError("runtime", PolicyReason.UNSUPPORTED)
    return CreatePlan(profile, request)
