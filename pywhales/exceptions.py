"""CLI failure types, confidential captured details, and stderr classification.

Exception messages withhold command and captured payload values by default.
Raw attributes remain available and confidential regardless of message policy.
"""

import re
from typing import Literal, override


class DockerException(Exception):
    """CLI failure retaining command details independently of diagnostic text.

    Safe messages expose the exit code but not argv or captured payloads. Raw
    diagnostic opt-in includes those values in exception text; it does not
    change the attributes or stderr-based exception classification.

    Attributes:
        docker_command: Launched arguments, retained without redaction.
        return_code: Local CLI exit code.
        stdout: Captured stdout decoded with replacement, or ``None`` when no
            captured value was supplied. May contain confidential data.
        stderr: Captured stderr decoded with replacement, or ``None`` when no
            captured value was supplied. May contain confidential data.
    """

    def __init__(
        self,
        command_launched: list[str],
        return_code: int,
        stdout: bytes | None = None,
        stderr: bytes | None = None,
        *,
        raw_diagnostics: bool = False,
    ) -> None:
        """Retain CLI details and construct safe or explicitly raw error text.

        Args:
            command_launched: Launched arguments retained as docker_command.
            return_code: Exit code of the local CLI process.
            stdout: Captured stdout bytes; ``None`` means no captured value.
            stderr: Captured stderr bytes; ``None`` means no captured value.
            raw_diagnostics: Include command and captured-output values in the
                message when true. Defaults to a safe summary without redacting
                the raw attributes.
        """
        self.docker_command: list[str] = command_launched
        self.return_code: int = return_code
        if stdout is None:
            self.stdout: str | None = None
        else:
            self.stdout: str | None = stdout.decode(errors="replace")
        if stderr is None:
            self.stderr: str | None = None
        else:
            self.stderr: str | None = stderr.decode(errors="replace")
        if not raw_diagnostics:
            super().__init__(
                f"The command returned with code {return_code}. "
                "Command and captured output details are withheld."
            )
            return
        command_launched_str = " ".join(command_launched)
        error_msg = (
            f"The command executed was `{command_launched_str}`.\n"
            f"It returned with code {return_code}\n"
        )
        if stdout is not None:
            error_msg += f"The content of stdout is '{self.stdout}'\n"
        else:
            error_msg += (
                "The content of stdout can be found above the "
                "stacktrace (it wasn't captured).\n"
            )
        if stderr is not None:
            error_msg += f"The content of stderr is '{self.stderr}'\n"
        else:
            error_msg += (
                "The content of stderr can be found above the "
                "stacktrace (it wasn't captured)."
            )
        super().__init__(error_msg)


class NoSuchContainer(DockerException):
    pass


class NoSuchImage(DockerException):
    pass


class NoSuchNetwork(DockerException):
    pass


class NoSuchService(DockerException):
    pass


class NotASwarmManager(DockerException):
    pass


class NoSuchVolume(DockerException):
    pass


class NoSuchConfig(DockerException):
    """The Swarm config named in the failed command does not exist."""


class NoSuchSecret(DockerException):
    """The Swarm secret named in the failed command does not exist."""


class NoSuchPlugin(DockerException):
    """The daemon plugin named in the failed command does not exist."""


class NoSuchContext(DockerException):
    """The Docker CLI context named in the failed command does not exist."""


class DockerTimeout(DockerException):
    """Local CLI timeout after launch-based expiry and process cleanup.

    Killing and reaping the local CLI does not guarantee cancellation of
    daemon-side work. Captured details follow DockerException's safe-message
    policy while remaining available through confidential raw attributes.
    """

    @override
    def __str__(self) -> str:
        """Return the timeout notice followed by policy-selected failure text."""
        return f"The local CLI exceeded its deadline.\n{super().__str__()}"


class ObservationLimitError(Exception):
    """Observation exceeded a limit without exposing command or payload values."""

    def __init__(self, reason: str, limit: int | None, measured: int | None) -> None:
        """Retain a machine-readable reason and optional numeric measurements."""
        self.reason = reason
        self.limit = limit
        self.measured = measured
        super().__init__(
            "An observation limit was exceeded or could not be determined."
        )


type StreamBufferCategory = Literal["output_record", "stderr_history"]


class StreamBufferLimitError(BufferError):
    """A stream record or retained stderr history exceeded ``buffer_bytes``.

    Derives from the builtin ``BufferError``, so existing handlers still catch
    every overflow. ``category`` names the exceeded bound as a closed value and
    never carries payload or message text.

    Attributes:
        category: ``output_record`` for one stdout or stderr record, or
            ``stderr_history`` for retained stderr.
    """

    def __init__(self, message: str, category: StreamBufferCategory) -> None:
        """Retain the overflow message and its closed category."""
        self.category: StreamBufferCategory = category
        super().__init__(message)


class ObservationTimeout(TimeoutError):
    """The observation's absolute monotonic deadline expired."""

    def __init__(self) -> None:
        """Construct a timeout without command or captured-output details."""
        super().__init__("The observation deadline expired.")


class ObservationCancelled(Exception):
    """An observation cancellation token was set."""

    def __init__(self) -> None:
        """Construct a cancellation without command or captured-output details."""
        super().__init__("The observation was cancelled.")


# Docker prefixes failures with the daemon marker, optionally led by one
# whitespace-free token (``docker:`` or a service name in ``service scale``) or
# one of two fixed CLI operation phrases, or with ``Error:``. After the marker
# the daemon may add its own ``cannot kill container: <name>: `` phrase. Each
# is allowed only at its listed position, never searched for elsewhere.
_CLI_OPERATION = r"(?:failed to rename container|could not fetch unlock key): "
_DAEMON_OPERATION = r"cannot kill container: \S+: "
_PREFIX = (
    rf"(?:(?:\S+: |{_CLI_OPERATION})?Error response from daemon: "
    rf"(?:{_DAEMON_OPERATION})?|Error: )?"
)
# Docker resource names contain no whitespace, so a name is one token.
_NAME = r"\S+"
_QUOTED_NAME = r'"[^"\s]+"'


def _line_start(*phrases: str) -> re.Pattern[str]:
    """Compile the alternatives of one kind, each anchored at a line start.

    Args:
        *phrases: Regular expressions for the text following the optional prefix.
            Each must end with a word character.

    Returns:
        A case-insensitive pattern matching a line that opens with the prefix and
        one of the phrases, ended on a word boundary.
    """
    alternatives = "|".join(phrases)
    return re.compile(rf"^{_PREFIX}(?:{alternatives})\b", re.IGNORECASE | re.MULTILINE)


# Evaluated in order; the first kind with a matching line wins, whatever the line
# order. The original kinds keep their historical order and the newer kinds follow.
_KIND_PATTERNS: tuple[tuple[type[DockerException], re.Pattern[str]], ...] = (
    (NoSuchImage, _line_start(r"no such image")),
    (
        NoSuchService,
        _line_start(r"no such service", rf"service {_NAME} not found"),
    ),
    (NoSuchContainer, _line_start(r"no such container")),
    (NotASwarmManager, _line_start(r"this node is not a swarm manager")),
    (
        NoSuchVolume,
        _line_start(r"no such volume", rf"get {_NAME}: no such volume"),
    ),
    (NoSuchNetwork, _line_start(rf"network {_NAME} not found")),
    (NoSuchConfig, _line_start(r"no such config", rf"config {_NAME} not found")),
    (NoSuchSecret, _line_start(r"no such secret", rf"secret {_NAME} not found")),
    (
        NoSuchPlugin,
        _line_start(r"no such plugin", rf"plugin {_QUOTED_NAME} not found"),
    ),
    (
        NoSuchContext,
        _line_start(
            rf"context {_QUOTED_NAME}: context not found",
            rf"context {_QUOTED_NAME} does not exist",
        ),
    ),
)


def get_docker_exception_type(output: bytes | None) -> type[DockerException]:
    """Select an exception class from the kind phrase opening a stderr line.

    A kind is recognized only when a line starts with its own phrase, compared
    case-insensitively after an optional ``Error response from daemon: ``,
    ``<token>: Error response from daemon: ``, or ``Error: `` prefix. The daemon
    marker may also follow ``failed to rename container: `` or ``could not fetch
    unlock key: `` and be followed by ``cannot kill container: <name>: ``. The phrases
    are ``No such <kind>`` for image, service, container, volume, config, secret,
    and plugin; ``<kind> <name> not found`` for service, network, config, and
    secret; ``get <name>: no such volume``; ``plugin "<name>" not found``;
    ``context "<name>": context not found``; ``context "<name>" does not exist``;
    and ``This node is not a swarm manager``. Resource names contain no
    whitespace and are matched as a single token after the phrase, so a name,
    or a file path inside a context message, can't supply another kind's phrase
    unless the path embeds a line break.

    When several lines name different kinds, the first kind in this order wins,
    regardless of line order: image, service, container, swarm manager, volume,
    network, config, secret, plugin, context. Unrecognized stderr, including a
    Swarm-mode ``volume <name> not found``, ``node <name> not found``, and
    ``no such task or service``, stays DockerException.

    The classifier reads whatever the CLI wrote to stderr. Application output
    that ``run`` or ``exec`` forwards there can still begin a line with one of
    these phrases and be misclassified. No attempt is made to prevent that.

    Args:
        output: Captured stderr bytes, or ``None`` when stderr was not captured.

    Returns:
        The specialized failure class of the highest-precedence matching kind, or
        DockerException for absent, empty, or unrecognized output. No exception
        is instantiated.
    """
    if output is None or len(output) == 0:
        return DockerException
    decoded = output.decode(errors="replace")
    for exception_type, pattern in _KIND_PATTERNS:
        if pattern.search(decoded) is not None:
            return exception_type
    return DockerException
