"""Compose option and ownership contracts with mocked commands and local children.

Subprocess execution is rejected by default. The child fixture temporarily
permits real Python launches for stream lifecycle checks, without using Docker.
"""

import sys
from collections.abc import Iterator
from dataclasses import fields
from inspect import Parameter, signature
from subprocess import CompletedProcess, Popen
from typing import Literal
from unittest.mock import Mock, patch

import pytest

from pywhales import Container, DockerClient
from pywhales.client_config import ClientConfig
from pywhales.exceptions import DockerException
from pywhales.utils import ProcessStream, stream_stdout_and_stderr


@pytest.fixture(autouse=True)
def deny_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject subprocess calls unless a narrower test fixture replaces the seam."""
    monkeypatch.setattr(
        "subprocess.run", Mock(side_effect=AssertionError("unexpected run"))
    )
    monkeypatch.setattr(
        "pywhales.utils.Popen", Mock(side_effect=AssertionError("unexpected Popen"))
    )


@pytest.fixture
def client() -> DockerClient:
    result = DockerClient(context="remote", host="ssh://engine", tls=True)
    result.client_config._client_call_with_path = ["docker-test", "--custom-wrapper"]
    return result


@pytest.fixture
def command(monkeypatch: pytest.MonkeyPatch) -> Mock:
    result = Mock(return_value=CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr("subprocess.run", result)
    return result


@pytest.fixture
def stream_command(monkeypatch: pytest.MonkeyPatch) -> Mock:
    result = Mock(return_value=Mock(spec=ProcessStream))
    monkeypatch.setattr(
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr", result
    )
    return result


@pytest.fixture
def child(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[ProcessStream]:
    """Start a real Python stream and close it when the fixture exits.

    Temporarily restore Popen only for launch, then reinstate the default denial.
    The default child prints readiness and waits for input.

    Args:
        monkeypatch: Scopes the temporary permission to launch a process.
        request: Optionally supplies replacement Python source through param.

    Yields:
        A fixture-owned stream with piped stdin, valid until teardown.
    """
    source = getattr(request, "param", "print('ready', flush=True); input()")
    with monkeypatch.context() as allowed:
        allowed.setattr("pywhales.utils.Popen", Popen)
        stream = stream_stdout_and_stderr(
            [sys.executable, "-c", source], pipe_stdin=True
        )
    with stream:
        yield stream


@pytest.mark.parametrize("method", ["build", "up"])
def test_default_argv_when_options_are_unset(
    client: DockerClient, command: Mock, method: str
) -> None:
    result = getattr(client.compose, method)()

    assert result is None
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "--custom-wrapper",
        "--context",
        "remote",
        "--host",
        "ssh://engine",
        "--tls",
        "compose",
        method,
    ]
    assert command.call_args.kwargs["stdout"] is None


@pytest.mark.parametrize("parallel", [0, -1, 4])
@pytest.mark.parametrize("progress", ["auto", "tty", "plain", "json", "quiet"])
def test_globals_precede_subcommand_when_explicit(
    parallel: int,
    progress: Literal["auto", "tty", "plain", "json", "quiet"],
    command: Mock,
) -> None:
    config = ClientConfig(compose_dry_run=True, compose_parallel=parallel)
    config.compose_progress = progress
    config._client_call_with_path = ["docker-test", "--custom-wrapper"]
    project = DockerClient(client_config=config)

    project.compose.build("web")

    assert command.call_args.args[0] == [
        "docker-test",
        "--custom-wrapper",
        "compose",
        "--dry-run",
        "--parallel",
        str(parallel),
        "--progress",
        progress,
        "build",
        "web",
    ]


def test_constructor_forwards_globals_when_given(command: Mock) -> None:
    project = DockerClient(
        compose_dry_run=True, compose_parallel=-1, compose_progress="json"
    )
    project.client_config._client_call_with_path = ["docker-test"]

    project.compose.up()

    assert command.call_args.args[0] == [
        "docker-test",
        "compose",
        "--dry-run",
        "--parallel",
        "-1",
        "--progress",
        "json",
        "up",
    ]


def test_constructor_accepts_current_positional_slots() -> None:
    project = DockerClient(
        "config",
        "context",
        True,
        "host",
        "info",
        True,
        "ca",
        "cert",
        "key",
        True,
        None,
        ["compose.yml"],
        ["dev"],
        ["dev.env"],
        "project",
        ".",
        True,
        ["docker-test", "--custom-wrapper"],
        "docker",
    )
    config = project.client_config
    config._client_call_with_path = ["docker-test", "--custom-wrapper"]

    assert config.docker_compose_cmd == [
        "docker-test",
        "--custom-wrapper",
        "--config",
        "config",
        "--context",
        "context",
        "--debug",
        "--host",
        "host",
        "--log-level",
        "info",
        "--tls",
        "--tlscacert",
        "ca",
        "--tlscert",
        "cert",
        "--tlskey",
        "key",
        "--tlsverify",
        "compose",
        "--file",
        "compose.yml",
        "--profile",
        "dev",
        "--env-file",
        "dev.env",
        "--project-name",
        "project",
        "--project-directory",
        ".",
        "--compatibility",
    ]
    assert [field.name for field in fields(ClientConfig)][-14:] == [
        "_client_call_with_path",
        "compose_dry_run",
        "compose_parallel",
        "compose_progress",
        "image_stream_buffer_bytes",
        "stream_buffer_bytes",
        "raw_diagnostics",
        "telemetry_observer",
        "environment",
        "default_timeout",
        "strict_env_transport",
        "inherit_environment",
        "strict_workload_errors",
        "inherited_environment_names",
    ]


def test_build_retains_positional_slots_when_modern_options_are_added(
    client: DockerClient, command: Mock
) -> None:
    client.compose.build(
        "web",
        {"VERSION": "1"},
        False,
        "plain",
        True,
        False,
        True,
        "default",
        False,
        builder="ci",
        check=True,
        print=True,
        push=True,
    )

    assert command.call_args.args[0][8:] == [
        "--progress",
        "plain",
        "build",
        "--build-arg",
        "VERSION",
        "--no-cache",
        "--pull",
        "--ssh",
        "default",
        "--with-dependencies",
        "--builder",
        "ci",
        "--check",
        "--print",
        "--push",
        "web",
    ]
    assert command.call_args.kwargs["env"]["VERSION"] == "1"


@pytest.mark.parametrize("attestation", [False, True, "mode=max"])
def test_attestations_are_explicit_when_set(
    client: DockerClient, command: Mock, attestation: bool | str
) -> None:
    client.compose.build(provenance=attestation, sbom=attestation)

    value = str(attestation).lower() if isinstance(attestation, bool) else attestation
    assert command.call_args.args[0][-2:] == [
        f"--provenance={value}",
        f"--sbom={value}",
    ]


def test_build_progress_is_local_when_overridden(
    client: DockerClient, command: Mock
) -> None:
    client.client_config.compose_progress = "json"
    sibling = DockerClient(client_config=client.client_config)

    client.compose.build(progress="plain")
    sibling.compose.up()

    assert command.call_args_list[0].args[0][8:] == ["--progress", "plain", "build"]
    assert command.call_args_list[1].args[0][8:] == ["--progress", "json", "up"]
    assert client.client_config.compose_progress == "json"


@pytest.mark.parametrize("stream", [False, True])
def test_print_preserves_return_convention_when_building(
    client: DockerClient, command: Mock, stream_command: Mock, stream: bool
) -> None:
    result = client.compose.build(print=True, stream_logs=stream)

    if stream:
        assert result is stream_command.return_value
        assert stream_command.call_args.args[0][-2:] == ["build", "--print"]
        command.assert_not_called()
    else:
        assert result is None
        assert command.call_args.args[0][-2:] == ["build", "--print"]
        assert command.call_args.kwargs["stdout"] is None
        stream_command.assert_not_called()


@pytest.mark.parametrize("method", ["build", "up", "watch"])
def test_empty_selection_is_noop_when_services_are_empty(
    client: DockerClient, command: Mock, stream_command: Mock, method: str
) -> None:
    client.client_config._client_call_with_path = None

    with patch.object(
        ClientConfig,
        "_get_docker_path",
        side_effect=AssertionError("unexpected lookup"),
    ):
        assert getattr(client.compose, method)([]) is None
    command.assert_not_called()
    stream_command.assert_not_called()


@pytest.mark.parametrize("method", ["build", "up"])
def test_quiet_stream_is_rejected_before_execution(
    client: DockerClient, command: Mock, stream_command: Mock, method: str
) -> None:
    with pytest.raises(ValueError):
        getattr(client.compose, method)(quiet=True, stream_logs=True)

    command.assert_not_called()
    stream_command.assert_not_called()


def test_up_retains_positional_slots_when_controls_are_added(
    client: DockerClient, command: Mock
) -> None:
    client.compose.up(
        "web",
        True,
        False,
        True,
        {"web": 0},
        True,
        True,
        False,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        ["worker"],
        "never",
        False,
        0,
        False,
        True,
        watch=True,
        abort_on_container_failure=True,
        exit_code_from="web",
    )

    assert command.call_args.args[0][8:] == [
        "up",
        "--build",
        "--wait-timeout",
        "0",
        "--abort-on-container-exit",
        "--scale",
        "web=0",
        "--attach-dependencies",
        "--force-recreate",
        "--no-recreate",
        "--no-build",
        "--no-color",
        "--no-log-prefix",
        "--no-start",
        "--remove-orphans",
        "--renew-anon-volumes",
        "--no-deps",
        "--yes",
        "--pull",
        "never",
        "--watch",
        "--abort-on-container-failure",
        "--exit-code-from",
        "web",
        "--no-attach",
        "worker",
        "web",
    ]


@pytest.mark.parametrize(
    "method, options",
    [
        ("build", ("builder", "check", "print", "provenance", "sbom", "push")),
        ("up", ("watch", "abort_on_container_failure", "exit_code_from")),
    ],
)
def test_new_method_options_are_keyword_only(
    method: str, options: tuple[str, ...]
) -> None:
    parameters = signature(getattr(DockerClient().compose, method)).parameters

    assert all(parameters[name].kind is Parameter.KEYWORD_ONLY for name in options)


@pytest.mark.parametrize("operation", ["ps", "run"])
def test_resource_return_is_rejected_when_dry_run(
    client: DockerClient, command: Mock, operation: str
) -> None:
    client.client_config.compose_dry_run = True

    with pytest.raises(ValueError, match=r"dry.run"):
        if operation == "ps":
            client.compose.ps()
        else:
            client.compose.run("web", detach=True, tty=False)

    command.assert_not_called()


def test_detached_run_retains_client_when_not_dry_run(
    client: DockerClient, command: Mock
) -> None:
    command.return_value = CompletedProcess([], 0, b"container-id\n", b"")

    result = client.compose.run("web", detach=True, tty=False)

    assert isinstance(result, Container)
    assert result.id == "container-id"
    assert result.client_config is client.client_config


def test_run_returns_preview_text_when_dry_run_is_attached(
    client: DockerClient, command: Mock
) -> None:
    client.client_config.compose_dry_run = True
    command.return_value = CompletedProcess([], 0, b"would create web\n", b"")

    result = client.compose.run("web", tty=False)

    assert result == "would create web"
    assert command.call_args.args[0][-4:] == ["--dry-run", "run", "--no-TTY", "web"]


def test_ps_retains_container_ids_when_not_dry_run(
    client: DockerClient, command: Mock
) -> None:
    command.return_value = CompletedProcess([], 0, b"first\nsecond\n", b"")

    result = client.compose.ps(["web"], all=True)

    assert [container.id for container in result] == ["first", "second"]
    assert all(container.client_config is client.client_config for container in result)
    assert command.call_args.args[0][-4:] == ["ps", "--quiet", "--all", "web"]


def test_config_retains_raw_data_when_globals_are_enabled(
    client: DockerClient, command: Mock
) -> None:
    client.client_config.compose_dry_run = True
    command.return_value = CompletedProcess(
        [], 0, b'{"services":{},"extension":{"custom":true}}', b""
    )

    result = client.compose.config(return_json=True)

    assert result == {"services": {}, "extension": {"custom": True}}
    assert command.call_args.args[0][-4:] == ["--dry-run", "config", "--format", "json"]


@pytest.mark.parametrize("method", ["build", "up"])
def test_cli_failure_propagates_when_command_fails(
    client: DockerClient, command: Mock, method: str
) -> None:
    command.return_value = CompletedProcess([], 9, b"", b"unsupported option")

    with pytest.raises(DockerException) as raised:
        getattr(client.compose, method)()

    assert raised.value.return_code == 9
    assert raised.value.stderr == "unsupported option"


@pytest.mark.parametrize("prune", [False, True])
def test_watch_forwards_controls_when_explicit(
    client: DockerClient, stream_command: Mock, prune: bool
) -> None:
    client.client_config.compose_parallel = 0
    client.client_config.compose_progress = "json"

    result = client.compose.watch(
        ["web", "worker"], no_up=True, quiet=True, prune=prune
    )

    assert result is stream_command.return_value
    assert stream_command.call_args.args[0] == [
        "docker-test",
        "--custom-wrapper",
        "--context",
        "remote",
        "--host",
        "ssh://engine",
        "--tls",
        "compose",
        "--parallel",
        0,
        "--progress",
        "json",
        "watch",
        "--no-up",
        "--quiet",
        f"--prune={str(prune).lower()}",
        "web",
        "worker",
    ]


@pytest.mark.parametrize("consume", [False, True])
@pytest.mark.parametrize("explicit_close", [False, True])
def test_watch_reaps_owned_child_when_closed(
    client: DockerClient, child: ProcessStream, consume: bool, explicit_close: bool
) -> None:
    with patch(
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr",
        return_value=child,
    ) as start:
        stream = client.compose.watch()
    assert stream is not None
    with stream:
        if consume:
            assert next(stream) == ("stdout", b"ready\n")
        if explicit_close:
            stream.close()
            stream.close()

    assert child._process.poll() is not None
    assert all(not thread.is_alive() for thread in child._threads)
    assert child._process.stdout is not None and child._process.stdout.closed
    assert child._process.stderr is not None and child._process.stderr.closed
    assert start.call_args.args[0][-2:] == ["watch", "--prune=false"]


@pytest.mark.parametrize("child", ["print('updated')"], indirect=True)
def test_watch_finishes_when_child_exits_successfully(
    client: DockerClient, child: ProcessStream
) -> None:
    with patch(
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr",
        return_value=child,
    ):
        stream = client.compose.watch("web")
    assert stream is not None

    with stream:
        output = list(stream)

    assert output == [("stdout", b"updated\n")]
    assert child._process.poll() == 0


def test_watch_preserves_consumer_error_when_context_exits(
    client: DockerClient, child: ProcessStream
) -> None:
    with patch(
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr",
        return_value=child,
    ):
        stream = client.compose.watch("web")
    assert stream is not None
    error = RuntimeError("consumer failed")

    with pytest.raises(RuntimeError) as raised, stream:
        raise error

    assert raised.value is error
    assert child._process.poll() is not None


@pytest.mark.parametrize(
    "child",
    ["import sys; print('updated'); sys.stderr.write('watch failed\\n'); sys.exit(7)"],
    indirect=True,
)
def test_watch_propagates_error_when_child_exits(
    client: DockerClient, child: ProcessStream
) -> None:
    with patch(
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr",
        return_value=child,
    ):
        stream = client.compose.watch("web")
    assert stream is not None

    with pytest.raises(DockerException) as raised, stream:
        list(stream)

    assert raised.value.return_code == 7
    assert raised.value.stderr == "watch failed\n"
    assert child._process.poll() == 7
