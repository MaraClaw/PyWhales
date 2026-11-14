"""Encode the service spec options shared by ``service create`` and ``service update``.

One ordered table describes every shared option. The create form emits
``--option``; the update form emits ``--option-add`` and ``--option-rm`` for list
options, and repeats the plain flag for options the CLI does not pair. Encoding is
pure argument assembly, so callers run it before resolving the CLI executable and
invalid values fail without a launch.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from pywhales._environment import container_environment
from pywhales.client_config import ClientConfig, Command
from pywhales.components.service._update_options import _duration
from pywhales.utils import (
    ValidPortMapping,
    format_mapping_for_cli,
    format_port_arg,
    to_list,
    to_seconds,
)

type ServiceModeInput = Literal["replicated", "global", "replicated-job", "global-job"]
type ServiceLongOption = Mapping[str, str | int]
"""One comma-separated long-form option, such as a mount, secret or config."""
type ServicePort = ValidPortMapping | ServiceLongOption
"""A published port as a port tuple or as long-form ``published``/``target`` keys."""
type SpecForm = Literal["create", "update"]

type _Encode = Callable[[Command, str, object], None]
type _Convert = Callable[[object], object]

_MODES: Final = ("replicated", "global", "replicated-job", "global-job")


def _plain(value: object) -> object:
    return value


def _scalar(convert: _Convert = _plain) -> _Encode:
    def encode(command: Command, flag: str, value: object) -> None:
        if value is not None:
            command.add_simple_arg(flag, convert(value))

    return encode


def _switch(command: Command, flag: str, value: object) -> None:
    command.add_flag(flag, bool(value))


def _tristate(command: Command, flag: str, value: object) -> None:
    if value is not None:
        command.append(flag if value else f"{flag}=false")


def _repeat(convert: _Convert = _plain) -> _Encode:
    def encode(command: Command, flag: str, value: object) -> None:
        if value is not None:
            command.add_args_iterable_or_single(
                flag, [convert(item) for item in to_list(value)]
            )

    return encode


_REPEAT: Final = _repeat()


def _mapping(separator: str = "=") -> _Encode:
    def encode(command: Command, flag: str, value: object) -> None:
        if value is not None:
            if not isinstance(value, Mapping):
                raise TypeError(f"{flag} must be a mapping")
            command.add_args_mapping(flag, value, separator=separator)

    return encode


def _long_form(value: object) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("long-form options must be mappings")
    return ",".join(format_mapping_for_cli(value))


def _port(value: object) -> str:
    if isinstance(value, Mapping):
        return ",".join(format_mapping_for_cli(value))
    if isinstance(value, tuple):
        return format_port_arg(value)
    raise TypeError("ports must be tuples or mappings")


def _duration_value(value: object) -> int | timedelta:
    if not isinstance(value, int | timedelta):
        raise TypeError("duration must be integer seconds or timedelta")
    return value


def _seconds(value: object) -> str | None:
    return to_seconds(_duration_value(value))


def _nanoseconds(value: object) -> str:
    return _duration(_duration_value(value))


def _mode(value: object) -> object:
    if value not in _MODES:
        raise ValueError("invalid service mode")
    return value


def _command_arguments(command: Command, flag: str, value: object) -> None:
    if value is not None:
        if isinstance(value, str | bytes):
            raise TypeError(f"{flag} must be a sequence of strings")
        command.add_simple_arg(flag, shlex.join(str(item) for item in to_list(value)))


@dataclass(frozen=True, slots=True)
class _Spec:
    """One shared option.

    Attributes:
        stem: Underscore spelling of the create flag, which is also its slot name.
        encode: Encoder for the create flag and for the update ``-add`` flag.
        shape: ``same`` options use one flag in both forms, ``paired`` options
            become ``-add``/``-rm`` in the update form, and ``create`` or
            ``update`` options exist in that form only.
        remove: Encoder for the update ``-rm`` flag of a paired option; the
            names or keys to drop are plain strings unless stated.
        update_encode: Replacement encoder for the update form of a same-shaped
            option, used for tri-state booleans.
        environment: Route values through the container environment transport.
    """

    stem: str
    encode: _Encode
    shape: Literal["same", "paired", "create", "update"] = "same"
    remove: _Encode = _REPEAT
    update_encode: _Encode | None = None
    environment: bool = False


@dataclass(frozen=True, slots=True)
class _Slot:
    name: str
    flag: str
    encode: _Encode
    environment: bool = False


# Create-form order keeps the argv of the options that predate this table. Update
# emits force, image, registry auth, detach and quiet itself, in its own order.
_SPECS: Final = (
    _Spec("cap_add", _REPEAT),
    _Spec("cap_drop", _REPEAT),
    _Spec("config", _repeat(_long_form), "paired"),
    _Spec("constraint", _REPEAT, "paired"),
    _Spec("container_label", _mapping(), "paired"),
    _Spec("credential_spec", _scalar()),
    _Spec("detach", _switch, "create"),
    _Spec("dns", _REPEAT, "paired"),
    _Spec("dns_option", _REPEAT, "paired"),
    _Spec("dns_search", _REPEAT, "paired"),
    _Spec("endpoint_mode", _scalar()),
    _Spec("entrypoint", _scalar()),
    _Spec("env", _REPEAT, "paired", environment=True),
    _Spec("env_file", _REPEAT, "create"),
    _Spec("generic_resource", _REPEAT, "paired"),
    _Spec("group", _REPEAT, "paired"),
    _Spec("no_healthcheck", _switch),
    _Spec("health_cmd", _scalar()),
    _Spec("health_interval", _scalar(_seconds)),
    _Spec("health_retries", _scalar()),
    _Spec("health_start_interval", _scalar(_seconds)),
    _Spec("health_start_period", _scalar(_seconds)),
    _Spec("health_timeout", _scalar(_seconds)),
    _Spec("host", _mapping(":"), "paired", _mapping(":")),
    _Spec("hostname", _scalar()),
    _Spec("init", _switch, update_encode=_tristate),
    _Spec("isolation", _scalar()),
    _Spec("label", _mapping(), "paired"),
    _Spec("limit_cpu", _scalar()),
    _Spec("limit_memory", _scalar()),
    _Spec("limit_pids", _scalar()),
    _Spec("log_driver", _scalar()),
    _Spec("log_opt", _mapping()),
    _Spec("max_concurrent", _scalar()),
    _Spec("memory_swap", _scalar()),
    _Spec("memory_swappiness", _scalar()),
    _Spec("mode", _scalar(_mode), "create"),
    _Spec("name", _scalar(), "create"),
    _Spec("no_resolve_image", _switch),
    _Spec("oom_score_adj", _scalar()),
    _Spec("placement_pref", _REPEAT, "paired"),
    _Spec("publish", _repeat(_port), "paired", _repeat(_port)),
    _Spec("read_only", _switch, update_encode=_tristate),
    _Spec("replicas", _scalar()),
    _Spec("replicas_max_per_node", _scalar()),
    _Spec("reserve_cpu", _scalar()),
    _Spec("reserve_memory", _scalar()),
    _Spec("restart_condition", _scalar()),
    _Spec("restart_delay", _scalar(_nanoseconds)),
    _Spec("restart_max_attempts", _scalar()),
    _Spec("restart_window", _scalar(_nanoseconds)),
    _Spec("mount", _repeat(_long_form), "paired"),
    _Spec("network", _REPEAT, "paired"),
    _Spec("secret", _repeat(_long_form), "paired"),
    _Spec("stop_grace_period", _scalar(_nanoseconds)),
    _Spec("stop_signal", _scalar()),
    _Spec("sysctl", _mapping(), "paired"),
    _Spec("tty", _switch, update_encode=_tristate),
    _Spec("ulimit", _REPEAT, "paired"),
    _Spec("user", _scalar()),
    _Spec("with_registry_auth", _switch, "create"),
    _Spec("workdir", _scalar()),
    _Spec("args", _command_arguments, "update"),
    _Spec("rollback", _switch, "update"),
)


def _flag(slot_name: str) -> str:
    return "--" + slot_name.replace("_", "-")


def _expand(form: SpecForm) -> tuple[_Slot, ...]:
    slots: list[_Slot] = []
    for spec in _SPECS:
        if spec.shape == ("update" if form == "create" else "create"):
            continue
        if form == "update" and spec.shape == "paired":
            slots.append(
                _Slot(
                    f"{spec.stem}_add",
                    _flag(f"{spec.stem}_add"),
                    spec.encode,
                    spec.environment,
                )
            )
            slots.append(
                _Slot(f"{spec.stem}_rm", _flag(f"{spec.stem}_rm"), spec.remove)
            )
        else:
            encode = spec.update_encode if form == "update" else None
            slots.append(
                _Slot(
                    spec.stem,
                    _flag(spec.stem),
                    encode or spec.encode,
                    spec.environment,
                )
            )
    return tuple(slots)


CREATE_SLOTS: Final = _expand("create")
UPDATE_SLOTS: Final = _expand("update")


@dataclass(frozen=True, slots=True)
class SpecArguments:
    """Encoded option arguments with the child environment they depend on.

    Attributes:
        arguments: Flags and values in table order, without the executable,
            subcommand or positional arguments.
        environment: Per-launch overrides bound to the environment snapshot taken
            while encoding; pass it to the launching call unchanged.
    """

    arguments: Command
    environment: dict[str, str]


def encode_options(
    form: SpecForm, values: Mapping[str, object], config: ClientConfig
) -> SpecArguments:
    """Encode option values into CLI arguments for one form.

    Args:
        form: ``create`` emits plain flags; ``update`` emits ``-add``/``-rm`` flags.
        values: Option values keyed by slot name. A missing slot or ``None`` omits
            its option; explicit zero, ``False`` and empty strings are kept where
            the CLI accepts them.
        config: Client whose environment decides how ``env`` values are transported.

    Returns:
        The arguments in table order and the child environment overrides.

    Raises:
        TypeError: If a value has the wrong shape or type.
        ValueError: If a value is outside its accepted set, or strict environment
            transport cannot keep an assignment out of argv.
    """
    arguments = Command()
    environment: dict[str, str] = {}
    for slot in CREATE_SLOTS if form == "create" else UPDATE_SLOTS:
        value = values.get(slot.name)
        if slot.environment:
            if value is not None and not isinstance(value, Mapping):
                raise TypeError(f"{slot.flag} must be a mapping")
            tokens, environment = container_environment(value or {}, config)
            arguments.add_args_iterable(slot.flag, tokens)
        else:
            slot.encode(arguments, slot.flag, value)
    return SpecArguments(arguments, environment)
