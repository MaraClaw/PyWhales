"""Validate and snapshot child environments without changing process state."""

import os
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pywhales._execution_policy import ExecutionPolicySource

_WINDOWS = os.name == "nt"
_frozen_environment: ContextVar[tuple[object, Mapping[str, str]] | None] = ContextVar(
    "pywhales_frozen_environment", default=None
)

# These names influence the CLI, its runtime, plugins, or credential helpers.
_CLI_PREFIXES = (
    "DOCKER_",
    "COMPOSE_",
    "BUILDX_",
    "BUILDKIT_",
    "XDG_",
    "SSH_",
    "GIT_",
    "OTEL_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "NODE_",
)
_CLI_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "FTP_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "KUBECONFIG",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "FORCE_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "TZ",
        "GODEBUG",
        "GOMAXPROCS",
        "GOGC",
        "GOMEMLIMIT",
        "GOTRACEBACK",
        "GOENV",
        "GOTOOLCHAIN",
        "PYWHALES_DEBUG",
        "ENV",
        "BASH_ENV",
        "IFS",
        "CDPATH",
        "SHELL",
        "SHELLOPTS",
        "BASHOPTS",
    }
)


class _ContainerEnvironment(dict[str, str]):
    def __init__(self, effective: Mapping[str, str]) -> None:
        super().__init__()
        self.effective = MappingProxyType(dict(effective))


def container_environment[K](
    envs: Mapping[K, object], source: ExecutionPolicySource
) -> tuple[list[str], dict[str, str]]:
    """Split container or build assignments into arguments and safe child overrides.

    Preserve iteration order and literal formatting for reserved, invalid, or
    colliding names. Inspect a matching frozen environment without consuming its
    one-use launch snapshot. Windows-equivalent entries all remain literal, so
    none can overwrite another. Values use the existing CLI string formatting.
    Bind the collision snapshot to the overrides so launch cannot select a
    different environment after the decision.

    Args:
        envs: Container assignments, including historically stringifiable values.
        source: Originating config whose effective child environment is consulted.

    Returns:
        Ordered values for repeated ``--env`` or ``--build-arg`` options and
        per-launch overrides.

    Raises:
        ValueError: If strict_env_transport requires an entry to stay out of argv
            but its name or value requires literal fallback. No entry is disclosed.
    """
    entries = [(key, f"{key}", f"{value}") for key, value in envs.items()]
    scope = _frozen_environment.get()
    frozen = scope[1] if scope is not None and scope[0] is source else None
    effective = resolve_environment(
        source.environment if frozen is None else None, frozen=frozen
    )
    names = {key.upper() if _WINDOWS else key for key in effective}
    counts = Counter(name.upper() if _WINDOWS else name for _, name, _ in entries)
    arguments: list[str] = []
    overrides = _ContainerEnvironment(effective)
    for key, name, value in entries:
        folded = name.upper() if _WINDOWS else name
        reserved = name.upper()
        if (
            not isinstance(key, str)
            or not name
            or "=" in name
            or "\0" in name
            or "\0" in value
            or folded in names
            or counts[folded] > 1
            or reserved in _CLI_NAMES
            or reserved.startswith((*_CLI_PREFIXES, "LC_"))
        ):
            if source.strict_env_transport:
                raise ValueError(
                    "strict_env_transport requires environment-only assignments"
                )
            arguments.append(f"{name}={value}")
        else:
            arguments.append(name)
            overrides[name] = value
    return arguments, overrides


def copy_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a validated copy without exposing invalid entry contents in errors."""
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping of strings")
    result: dict[str, str] = {}
    names: set[str] = set()
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("environment keys and values must be strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("invalid environment entry")
        name = key.upper() if _WINDOWS else key
        if name in names:
            raise ValueError("duplicate environment name")
        names.add(name)
        result[key] = value
    return result


def _merge_environment(base: dict[str, str], layer: Mapping[str, str]) -> None:
    if _WINDOWS:
        names = {key.upper(): key for key in base}
        for key, value in layer.items():
            previous = names.get(key.upper())
            if previous is not None:
                del base[previous]
            base[key] = value
            names[key.upper()] = key
    else:
        base.update(layer)


def resolve_environment(
    environment: Mapping[str, str] | None,
    overrides: Mapping[str, str] | None = None,
    *,
    frozen: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Select the full child environment before argument stringification.

    A frozen full environment replaces ambient and client layers; operation
    overrides still apply. Return a new mapping owned by the caller.
    """
    if isinstance(overrides, _ContainerEnvironment):
        frozen = overrides.effective
    if frozen is not None:
        result = dict(frozen)
    else:
        result = copy_environment(os.environ)
        if environment is not None:
            _merge_environment(result, copy_environment(environment))
    if overrides is not None:
        _merge_environment(result, copy_environment(overrides))
    return result


def finalize_environment(
    environment: Mapping[str, str], args: Sequence[str]
) -> dict[str, str]:
    """Apply mandatory Buildx settings after argv conversion without rereading inputs."""
    result = dict(environment)
    if len(args) > 1 and args[1] == "buildx":
        _merge_environment(result, {"DOCKER_CLI_EXPERIMENTAL": "enabled"})
    return result


@contextmanager
def frozen_environment_scope(
    source: object, environment: Mapping[str, str]
) -> Generator[None]:
    """Install a one-use full environment for the identical policy source.

    The next matching policy snapshot consumes the value, so nested launches
    triggered after that snapshot use their own current environment.
    """
    token = _frozen_environment.set(
        (source, MappingProxyType(copy_environment(environment)))
    )
    try:
        yield
    finally:
        _frozen_environment.reset(token)


def consume_frozen_environment(source: object) -> Mapping[str, str] | None:
    """Consume the planned environment only when its source is identical."""
    scope = _frozen_environment.get()
    if scope is None or scope[0] is not source:
        return None
    _frozen_environment.set(None)
    return scope[1]
