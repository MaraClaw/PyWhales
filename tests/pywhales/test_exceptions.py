import sys
from collections.abc import Mapping, Sequence
from unittest.mock import patch

import pytest

import pywhales.utils
from pywhales import Container, DockerClient
from pywhales.exceptions import (
    DockerException,
    NoSuchConfig,
    NoSuchContainer,
    NoSuchContext,
    NoSuchImage,
    NoSuchNetwork,
    NoSuchPlugin,
    NoSuchSecret,
    NoSuchService,
    NoSuchVolume,
    NotASwarmManager,
    get_docker_exception_type,
)
from pywhales.utils import ProcessStream


@pytest.mark.parametrize("raw", [False, True])
def test_exception_rendering_keeps_raw_attributes_when_policy_changes(
    raw: bool,
) -> None:
    secret = "exception-secret-sentinel"

    error = DockerException(
        ["engine", secret], 7, secret.encode(), secret.encode(), raw_diagnostics=raw
    )

    assert (secret in str(error)) is raw
    assert (secret in repr(error)) is raw
    assert error.docker_command == ["engine", secret]
    assert error.stdout == error.stderr == secret
    assert error.return_code == 7


def test_exception_rendering_is_safe_when_policy_is_omitted() -> None:
    error = DockerException(
        ["engine", "default-secret"], 9, b"default-secret", b"default-secret"
    )

    assert "default-secret" not in str(error)
    assert "9" in str(error)


def test_exception_attributes() -> None:
    with pytest.raises(DockerException) as excinfo:
        DockerClient(raw_diagnostics=True).image.tag("wrong::image", "wrong::tag")

    exception = excinfo.value
    assert "The command executed was" in str(exception)
    assert exception.docker_command[1:5] == [
        "image",
        "tag",
        "wrong::image",
        "wrong::tag",
    ]
    # The exact return code and error string might vary with future docker
    # versions, so following asserts are somewhat relaxed
    assert exception.return_code > 0
    assert not bool(exception.stdout)
    assert exception.stderr is not None
    assert "wrong::" in exception.stderr


def test_not_showing_password_in_exception() -> None:
    chosen_password = "ignore_password"
    with pytest.raises(DockerException) as excinfo:
        DockerClient(raw_diagnostics=True).login(
            username="ignore_user", password=chosen_password
        )
    exception = excinfo.value
    assert "The command executed was" in str(exception)
    assert chosen_password not in str(exception)
    assert chosen_password not in exception.docker_command
    assert exception.return_code > 0
    assert not bool(exception.stdout)


NOT_A_MANAGER = (
    "This node is not a swarm manager. "
    'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    "and try again."
)
CONTEXT_META = "/Users/me/.docker/contexts/meta/0123abcd/meta.json"

# The expected types below are hand-written from what the previous substring
# classifier returned, so they pin behavior that must not change.
UNCHANGED_MESSAGES = [
    pytest.param(b"Error response from daemon: No such container: X", NoSuchContainer),
    pytest.param(b"Error response from daemon: No such image: X:latest", NoSuchImage),
    pytest.param(b"Error response from daemon: get X: no such volume", NoSuchVolume),
    pytest.param(b"Error response from daemon: network X not found", NoSuchNetwork),
    pytest.param(
        b"Error response from daemon: network X not found\nexit status 1",
        NoSuchNetwork,
    ),
    pytest.param(b"no such service: X", NoSuchService),
    pytest.param(b"Error response from daemon: service X not found", NoSuchService),
    pytest.param(b"X: Error response from daemon: service X not found", NoSuchService),
    pytest.param(
        f"Error response from daemon: {NOT_A_MANAGER}".encode(), NotASwarmManager
    ),
    pytest.param(
        f"X: Error response from daemon: {NOT_A_MANAGER}".encode(), NotASwarmManager
    ),
    pytest.param(b"Error: No such container: X", NoSuchContainer),
    pytest.param(b"Error: No such image: X", NoSuchImage),
    pytest.param(b"Error: No such volume: X", NoSuchVolume),
    pytest.param(b"Error: No such service: X", NoSuchService),
    pytest.param(b"Error: No such container: absent", NoSuchContainer),
    pytest.param(b"No such container: resource", NoSuchContainer),
    pytest.param(b"No such image: resource", NoSuchImage),
    pytest.param(b"No such volume: resource", NoSuchVolume),
    pytest.param(b"network resource not found", NoSuchNetwork),
    pytest.param(b"no such service: absent", NoSuchService),
    pytest.param(b"no such service", NoSuchService),
    pytest.param(b"No such container", NoSuchContainer),
    pytest.param(b"No such image", NoSuchImage),
    pytest.param(b"No such container: missing\n", NoSuchContainer),
    pytest.param(b"NO SUCH IMAGE: X", NoSuchImage),
    pytest.param(b"ERROR: No such service: web", NoSuchService),
    pytest.param(b"Error: No such image or container: X", NoSuchImage),
    pytest.param(
        b"docker: Error response from daemon: network X not found.", NoSuchNetwork
    ),
    pytest.param(
        b"docker: Error response from daemon: No such container: X.", NoSuchContainer
    ),
    pytest.param(
        b"Error response from daemon: No such container:path: X:/etc", NoSuchContainer
    ),
    pytest.param(b"No such container: X\r\n", NoSuchContainer),
    pytest.param(b"\xff\nNo such image: X", NoSuchImage),
    # Several kinds in one stderr: the historical kind order decides.
    pytest.param(
        b"Error response from daemon: No such container: a\n"
        b"Error response from daemon: No such image: b",
        NoSuchImage,
    ),
    pytest.param(
        b"Error response from daemon: No such image: b\n"
        b"Error response from daemon: No such container: a",
        NoSuchImage,
    ),
    pytest.param(
        b"Error response from daemon: network a not found\n"
        b"Error response from daemon: get b: no such volume",
        NoSuchVolume,
    ),
    pytest.param(
        b"Error: failed to start containers: a\n"
        b"Error response from daemon: No such container: a",
        NoSuchContainer,
    ),
    # Unclassified before and after.
    pytest.param(b"", DockerException),
    pytest.param(b"permission denied", DockerException),
    pytest.param(b"secret error", DockerException),
    pytest.param(b"error: no such object: X", DockerException),
    pytest.param(b"Error: No such object: X", DockerException),
    pytest.param(b"no such task or service: X", DockerException),
    pytest.param(b"Error response from daemon: node X not found", DockerException),
    pytest.param(b"Error response from daemon: volume X not found", DockerException),
    pytest.param(
        b"Error response from daemon: manifest for X:latest not found: "
        b"manifest unknown",
        DockerException,
    ),
    pytest.param(b"hi: command not found", DockerException),
]


@pytest.mark.parametrize(("stderr", "expected"), UNCHANGED_MESSAGES)
def test_existing_messages_classify_as_before(
    stderr: bytes, expected: type[DockerException]
) -> None:
    assert get_docker_exception_type(stderr) is expected


def test_absent_stderr_is_a_plain_docker_exception() -> None:
    assert get_docker_exception_type(None) is DockerException
    assert get_docker_exception_type(b"") is DockerException


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(
            b"Error response from daemon: config X not found", NoSuchConfig, id="config"
        ),
        pytest.param(b"Error: No such config: X", NoSuchConfig, id="config-no-such"),
        pytest.param(b"no such config: X", NoSuchConfig, id="config-bare"),
        pytest.param(
            b"X: Error response from daemon: config X not found",
            NoSuchConfig,
            id="config-prefixed",
        ),
        pytest.param(
            b"Error response from daemon: secret X not found", NoSuchSecret, id="secret"
        ),
        pytest.param(b"Error: No such secret: X", NoSuchSecret, id="secret-no-such"),
        pytest.param(b"no such secret: X", NoSuchSecret, id="secret-bare"),
        pytest.param(
            b'Error response from daemon: plugin "X" not found',
            NoSuchPlugin,
            id="plugin",
        ),
        pytest.param(b"Error: No such plugin: X", NoSuchPlugin, id="plugin-no-such"),
        pytest.param(
            f'context "X": context not found: open {CONTEXT_META}: '
            "no such file or directory".encode(),
            NoSuchContext,
            id="context-inspect",
        ),
        pytest.param(b'context "X" does not exist', NoSuchContext, id="context-remove"),
        pytest.param(
            b'Error: context "X" does not exist', NoSuchContext, id="context-error"
        ),
    ],
)
def test_new_kinds_are_classified(
    stderr: bytes, expected: type[DockerException]
) -> None:
    assert get_docker_exception_type(stderr) is expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(
            b"Error response from daemon: cannot kill container: DOODODGOIHURHURI: "
            b"No such container: DOODODGOIHURHURI",
            NoSuchContainer,
            id="kill-real",
        ),
        pytest.param(
            b"failed to rename container: Error response from daemon: "
            b"No such container: dodueizbgueirzhgueoz",
            NoSuchContainer,
            id="rename-real",
        ),
        pytest.param(
            b"could not fetch unlock key: Error response from daemon: "
            b"This node is not a swarm manager. Use ...",
            NotASwarmManager,
            id="unlock-key-real",
        ),
        pytest.param(
            b"ERROR RESPONSE FROM DAEMON: CANNOT KILL CONTAINER: x: NO SUCH CONTAINER: x",
            NoSuchContainer,
            id="kill-case",
        ),
        pytest.param(
            b"noise\nfailed to rename container: Error response from daemon: "
            b"No such container: x\n",
            NoSuchContainer,
            id="rename-later-line",
        ),
        pytest.param(
            b"Error response from daemon: cannot kill container: a: "
            b"No such container: a\n"
            b"Error response from daemon: cannot kill container: b: No such image: b",
            NoSuchImage,
            id="kill-precedence",
        ),
    ],
)
def test_operation_wrappers_around_daemon_errors_are_classified(
    stderr: bytes, expected: type[DockerException]
) -> None:
    assert get_docker_exception_type(stderr) is expected


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            b"cannot kill container: x: No such container: x", id="kill-no-marker"
        ),
        pytest.param(
            b"Error: cannot kill container: x: No such container: x",
            id="kill-error-prefix",
        ),
        pytest.param(
            b"failed to rename container: No such container: x", id="rename-no-marker"
        ),
        pytest.param(
            b"failed to rename container: Error: No such container: x",
            id="rename-error-prefix",
        ),
        pytest.param(
            b"could not fetch unlock key: Error: This node is not a swarm manager",
            id="unlock-key-error-prefix",
        ),
        pytest.param(
            b"cannot kill container: Error response from daemon: No such container: x",
            id="kill-before-marker",
        ),
        pytest.param(
            b"Error response from daemon: failed to rename container: "
            b"No such container: x",
            id="rename-after-marker",
        ),
        pytest.param(
            b"prefix failed to rename container: Error response from daemon: "
            b"No such container: x",
            id="rename-mid-line",
        ),
        pytest.param(
            b"a b: failed to rename container: Error response from daemon: "
            b"No such container: x",
            id="rename-behind-extra-token",
        ),
        pytest.param(
            b"Error response from daemon: cannot kill container: a b: "
            b"No such container: x",
            id="kill-name-with-whitespace",
        ),
        pytest.param(
            b"Error response from daemon: cannot kill container: x: failed: "
            b"No such container: x",
            id="kill-two-tokens",
        ),
        pytest.param(
            b"Error response from daemon: cannot kill container: x: "
            b"Container x is not running",
            id="kill-other-failure",
        ),
        pytest.param(
            b"failed to rename container: Error response from daemon: "
            b"Conflict. The container name is in use",
            id="rename-other-failure",
        ),
    ],
)
def test_operation_wrappers_stay_anchored(stderr: bytes) -> None:
    assert get_docker_exception_type(stderr) is DockerException


@pytest.mark.parametrize(
    "error_type", [NoSuchConfig, NoSuchSecret, NoSuchPlugin, NoSuchContext]
)
def test_new_kinds_directly_extend_docker_exception(
    error_type: type[DockerException],
) -> None:
    assert error_type.__bases__ == (DockerException,)


def test_kind_precedence_ignores_line_order() -> None:
    lines = [
        ("No such image: a", NoSuchImage),
        ("no such service: a", NoSuchService),
        ("No such container: a", NoSuchContainer),
        (NOT_A_MANAGER, NotASwarmManager),
        ("get a: no such volume", NoSuchVolume),
        ("network a not found", NoSuchNetwork),
        ("config a not found", NoSuchConfig),
        ("secret a not found", NoSuchSecret),
        ('plugin "a" not found', NoSuchPlugin),
        ('context "a" does not exist', NoSuchContext),
    ]
    for index, (earlier_line, earlier) in enumerate(lines):
        for later_line, _ in lines[index + 1 :]:
            for first, second in (
                (earlier_line, later_line),
                (later_line, earlier_line),
            ):
                stderr = f"{first}\n{second}".encode()
                assert get_docker_exception_type(stderr) is earlier, stderr


TYPE_WORDS = (
    "image",
    "container",
    "volume",
    "network",
    "service",
    "config",
    "secret",
    "plugin",
    "context",
    "node",
    "task",
    "swarm",
    "manager",
)
# Names hold no whitespace, so the two-word phrases enter a name joined or split.
PHRASE_WORDS = (
    "not-found",
    "not_found",
    "notfound",
    "no-such",
    "no_such",
    "nosuch",
    "not",
    "found",
    "no",
    "such",
)
NAMES = sorted(
    {
        shaped
        for word in (*TYPE_WORDS, *PHRASE_WORDS)
        for shaped in (
            word,
            word.upper(),
            f"{word}-x",
            f"x-{word}",
            f"x_{word}_y",
            f"{word}:{word}",
        )
    }
    | {f"no-such-{word}" for word in TYPE_WORDS}
    | {f"{word}-not-found" for word in TYPE_WORDS}
)

# Every message form in use: (template, kind the line is about). A kind of
# DockerException marks a failure that no specialized class covers.
FORMS = [
    pytest.param("Error response from daemon: No such container: {}", NoSuchContainer),
    pytest.param("Error: No such container: {}", NoSuchContainer),
    pytest.param("No such container: {}", NoSuchContainer),
    pytest.param(
        "Error response from daemon: No such container:path: {}:/etc", NoSuchContainer
    ),
    pytest.param("Error response from daemon: No such image: {}:latest", NoSuchImage),
    pytest.param("Error: No such image: {}", NoSuchImage),
    pytest.param("No such image: {}", NoSuchImage),
    pytest.param("Error response from daemon: get {}: no such volume", NoSuchVolume),
    pytest.param("Error: No such volume: {}", NoSuchVolume),
    pytest.param("No such volume: {}", NoSuchVolume),
    pytest.param("Error response from daemon: volume {} not found", DockerException),
    pytest.param("Error response from daemon: network {} not found", NoSuchNetwork),
    pytest.param(
        "Error response from daemon: network {} not found\nexit status 1", NoSuchNetwork
    ),
    pytest.param("network {} not found", NoSuchNetwork),
    pytest.param(
        "docker: Error response from daemon: network {} not found.", NoSuchNetwork
    ),
    pytest.param("no such service: {}", NoSuchService),
    pytest.param("Error: No such service: {}", NoSuchService),
    pytest.param("Error response from daemon: service {} not found", NoSuchService),
    pytest.param(
        "{0}: Error response from daemon: service {0} not found", NoSuchService
    ),
    pytest.param("no such task or service: {}", DockerException),
    pytest.param("Error response from daemon: node {} not found", DockerException),
    pytest.param("error: no such object: {}", DockerException),
    pytest.param(
        "Error response from daemon: manifest for {} not found: manifest unknown",
        DockerException,
    ),
    pytest.param("Error response from daemon: config {} not found", NoSuchConfig),
    pytest.param("Error: No such config: {}", NoSuchConfig),
    pytest.param("{0}: Error response from daemon: config {0} not found", NoSuchConfig),
    pytest.param("Error response from daemon: secret {} not found", NoSuchSecret),
    pytest.param("Error: No such secret: {}", NoSuchSecret),
    pytest.param('Error response from daemon: plugin "{}" not found', NoSuchPlugin),
    pytest.param("Error: No such plugin: {}", NoSuchPlugin),
    pytest.param(
        'context "{}": context not found: open ' + CONTEXT_META + ": "
        "no such file or directory",
        NoSuchContext,
    ),
    pytest.param('context "{}" does not exist', NoSuchContext),
    pytest.param("{0}: Error response from daemon: " + NOT_A_MANAGER, NotASwarmManager),
    pytest.param(
        "Error response from daemon: cannot kill container: {0}: "
        "No such container: {0}",
        NoSuchContainer,
    ),
    pytest.param(
        "failed to rename container: Error response from daemon: No such container: {}",
        NoSuchContainer,
    ),
    pytest.param(
        "could not fetch unlock key: Error response from daemon: " + NOT_A_MANAGER,
        NotASwarmManager,
    ),
]


@pytest.mark.parametrize(("template", "expected"), FORMS)
def test_resource_name_cannot_forge_another_kind(
    template: str, expected: type[DockerException]
) -> None:
    misclassified = {
        name: actual.__name__
        for name in NAMES
        if (actual := get_docker_exception_type(template.format(name).encode()))
        is not expected
    }
    assert misclassified == {}


@pytest.mark.parametrize(
    "path",
    [
        CONTEXT_META,
        "/home/service/.docker/contexts/meta/0123abcd/meta.json",
        "/home/network not found/.docker/contexts/meta/0123abcd/meta.json",
        "/home/no such volume/.docker/contexts/meta/0123abcd/meta.json",
        "/home/service x not found/.docker/contexts/meta/0123abcd/meta.json",
        "/home/Error response from daemon: No such container: z/.docker/meta.json",
        '/home/context "y" does not exist/.docker/meta.json',
        "C:\\Users\\No such image\\.docker\\contexts\\meta\\0123abcd\\meta.json",
        "/home/secret x not found/.docker/contexts/meta/0123abcd/meta.json",
    ],
)
def test_context_file_path_cannot_forge_a_kind(path: str) -> None:
    stderr = (
        f'context "work": context not found: open {path}: no such file or directory'
    )
    assert get_docker_exception_type(stderr.encode()) is NoSuchContext


@pytest.mark.parametrize(
    ("operation", "specialized"),
    [
        ("container-run", False),
        ("container-run-stream", False),
        ("container-run-detached", True),
        ("container-exec", False),
        ("container-exec-stream", False),
        ("container-exec-detached", True),
        ("container-start", False),
        ("container-start-stream", False),
        ("container-start-unattached", True),
        ("handle-exec", False),
        ("handle-start", False),
        ("compose-run", False),
        ("compose-run-stream", False),
        ("compose-run-detached", True),
        ("compose-exec", False),
        ("compose-exec-detached", True),
        ("preflight", True),
        ("preflight-pull", True),
        ("image-inspect", True),
    ],
)
def test_workload_classification_at_wrapper_launch_boundary(
    operation: str, specialized: bool
) -> None:
    program = "import sys; print('Error: No such container: db', file=sys.stderr); sys.exit(125)"
    client = DockerClient(
        client_call=[sys.executable, "-c", program], strict_workload_errors=True
    )
    handle = Container(client.client_config, "db", is_immutable_id=True)

    # Container.run normally forwards stderr; capture it here to distinguish the
    # classification decision while still executing the actual local child.
    def capture_run(
        args: Sequence[object],
        *,
        env: Mapping[str, str] = {},
        pass_fds: Sequence[int] = (),
        timeout: float | None = None,
        tty: bool = False,
        capture_stderr: bool = True,
        capture_stdout: bool = True,
        workload_status: bool = False,
    ) -> str:
        return pywhales.utils.run(
            args,
            env=env,
            tty=tty,
            capture_stdout=capture_stdout,
            capture_stderr=True,
            pass_fds=pass_fds,
            timeout=timeout,
            workload_status=workload_status,
        )

    with (
        patch("pywhales.components.container.cli_wrapper.run", side_effect=capture_run),
        patch("pywhales.components.image.cli_wrapper.run", side_effect=capture_run),
        pytest.raises(DockerException) as raised,
    ):
        if operation == "container-run":
            result = client.run("image", pull="never")
        elif operation == "container-run-stream":
            result = client.run("image", pull="never", stream=True)
        elif operation == "container-run-detached":
            result = client.run("image", pull="never", detach=True)
        elif operation == "container-exec":
            result = client.execute("db", ["command"])
        elif operation == "container-exec-stream":
            result = client.execute("db", ["command"], stream=True)
        elif operation == "container-exec-detached":
            result = client.execute("db", ["command"], detach=True)
        elif operation == "container-start":
            result = client.start("db", attach=True)
        elif operation == "container-start-stream":
            result = client.start("db", attach=True, stream=True)
        elif operation == "container-start-unattached":
            result = client.start("db")
        elif operation == "handle-exec":
            result = handle.execute(["command"])
        elif operation == "handle-start":
            result = handle.start(attach=True)
        elif operation == "compose-run":
            result = client.compose.run("service", tty=False)
        elif operation == "compose-run-stream":
            result = client.compose.run("service", tty=False, stream=True)
        elif operation == "compose-run-detached":
            result = client.compose.run("service", tty=False, detach=True)
        elif operation == "compose-exec":
            result = client.compose.execute("service", ["command"], tty=False)
        elif operation == "compose-exec-detached":
            result = client.compose.execute(
                "service", ["command"], tty=False, detach=True
            )
        elif operation == "preflight":
            result = client.run("image", pull="missing")
        elif operation == "preflight-pull":
            result = client.run("image", pull="always")
        else:
            result = client.image.inspect("image")
        if isinstance(result, ProcessStream):
            with result:
                list(result)

    assert type(raised.value) is (NoSuchContainer if specialized else DockerException)
    assert raised.value.return_code == 125
    assert raised.value.stderr == "Error: No such container: db\n"
    if operation in {"preflight", "preflight-pull"}:
        assert raised.value.docker_command[-3:-1] == ["image", "pull"]
    elif operation == "image-inspect":
        assert raised.value.docker_command[-3:-1] == ["image", "inspect"]
