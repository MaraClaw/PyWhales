import json
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from inspect import Parameter, signature
from pathlib import Path
from typing import Literal, assert_never, get_overloads
from unittest.mock import Mock, PropertyMock

import pytest
from pytest_mock import MockerFixture

from pywhales import Container, DockerClient, Image
from pywhales._execution_policy import (
    PolicyExecutable,
    execution_timeout,
    get_execution_policy,
)
from pywhales.client_config import ClientConfig, DockerCLICaller
from pywhales.components.buildx import cli_wrapper as buildx
from pywhales.components.compose import cli_wrapper as compose
from pywhales.components.container import cli_wrapper as container
from pywhales.components.image import cli_wrapper as image
from pywhales.components.manifest import cli_wrapper as manifest
from pywhales.components.plugin import cli_wrapper as plugin
from pywhales.components.plugin.cli_wrapper import Plugin
from pywhales.components.service import cli_wrapper as service
from pywhales.components.service.cli_wrapper import Service
from pywhales.components.stack import cli_wrapper as stack
from pywhales.components.swarm import cli_wrapper as swarm
from pywhales.components.system import cli_wrapper as system
from pywhales.exceptions import DockerTimeout
from pywhales.utils import ProcessStream, run, stream_stdout_and_stderr


@pytest.fixture
def client() -> DockerClient:
    result = DockerClient()
    result.client_config._client_call_with_path = ["docker-test"]
    return result


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("operation", ["run", "execute", "compose"])
def test_selected_container_and_compose_budget_forwarding(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["run", "execute", "compose"],
    stream: bool,
    timeout: float | None,
) -> None:
    module = compose if operation == "compose" else container
    execute = mocker.patch.object(
        module, "stream_stdout_and_stderr" if stream else "run", return_value="result"
    )

    match operation:
        case "run":
            client.run("app", pull="never", stream=stream, timeout=timeout)
        case "execute":
            client.execute(
                "app", ["cmd"], interactive=True, stream=stream, timeout=timeout
            )
        case "compose":
            client.compose.run("app", tty=False, stream=stream, timeout=timeout)
        case unreachable:
            assert_never(unreachable)

    execute.assert_called_once()
    assert execute.call_args.kwargs["timeout"] == timeout
    if operation != "compose":
        assert execute.call_args.kwargs["pass_fds"] == ()
    if operation == "execute" and stream:
        assert execute.call_args.kwargs["pipe_stdin"] is True
    if not stream:
        assert execute.call_args.kwargs["tty"] is False


@pytest.mark.parametrize("pull", ["missing", "always"])
def test_container_pull_is_outside_selected_budget(
    client: DockerClient, mocker: MockerFixture, pull: Literal["missing", "always"]
) -> None:
    image = mocker.patch("pywhales.components.image.cli_wrapper.ImageCLI").return_value
    execute = mocker.patch.object(container, "run", return_value="done")

    result = client.run("app", pull=pull, timeout=2.5)

    assert result == "done"
    if pull == "missing":
        image._pull_if_necessary.assert_called_once_with("app")
        image.pull.assert_not_called()
    else:
        image.pull.assert_called_once_with("app")
        image._pull_if_necessary.assert_not_called()
    assert execute.call_args.kwargs["timeout"] == 2.5


@pytest.mark.parametrize("driver", ["docker", "docker-container"])
@pytest.mark.parametrize("expire", [False, True])
def test_build_inspections_are_unbudgeted_and_iidfile_is_cleaned(
    client: DockerClient, mocker: MockerFixture, driver: str, expire: bool
) -> None:
    iidfiles: list[Path] = []

    def execute(args: Sequence[object], **kwargs: object) -> str:
        command = [str(arg) for arg in args]
        match command[2]:
            case "inspect":
                assert "timeout" not in kwargs
                return "Name: selected\n"
            case "ls":
                assert "timeout" not in kwargs
                return json.dumps({"Name": "selected", "Driver": driver})
            case "build":
                assert kwargs["timeout"] == 2.5
                if "--iidfile" in command:
                    iidfile = Path(command[command.index("--iidfile") + 1])
                    iidfiles.append(iidfile)
                    iidfile.write_text("sha256:built")
                if expire:
                    raise DockerTimeout(command, -9)
                return "done"
            case _:
                pytest.fail(f"Unexpected command: {command}")

    commands = mocker.patch.object(buildx, "run", side_effect=execute)
    image = Image(client.client_config, "sha256:built", is_immutable_id=True)
    inspect = mocker.patch(
        "pywhales.components.image.cli_wrapper.ImageCLI.inspect", return_value=image
    )

    if expire:
        with pytest.raises(DockerTimeout):
            client.build(".", load=True, tags="app", timeout=2.5)
        inspect.assert_not_called()
    else:
        assert client.build(".", load=True, tags="app", timeout=2.5) is image
        inspect.assert_called_once_with("sha256:built" if driver == "docker" else "app")

    assert commands.call_count == 3
    assert len(iidfiles) == (1 if driver == "docker" else 0)
    assert all(not path.parent.exists() for path in iidfiles)


@pytest.mark.parametrize("mode", ["push", "export", "report"])
def test_build_without_inspections_receives_selected_budget(
    client: DockerClient,
    mocker: MockerFixture,
    mode: Literal["push", "export", "report"],
) -> None:
    commands = mocker.patch.object(buildx, "run", return_value="report")
    inspect = mocker.patch.object(
        buildx.BuildxCLI, "inspect", side_effect=AssertionError("unexpected inspection")
    )

    match mode:
        case "push":
            result = client.build(".", push=True, timeout=2.5)
        case "export":
            result = client.build(
                ".", output={"type": "local", "dest": "out"}, timeout=2.5
            )
        case "report":
            result = client.build(".", call="check", timeout=2.5)
        case unreachable:
            assert_never(unreachable)

    assert result == ("report" if mode == "report" else None)
    commands.assert_called_once()
    assert commands.call_args.kwargs["timeout"] == 2.5
    inspect.assert_not_called()


@pytest.mark.parametrize("call", [None, "build", "check", "outline", "targets"])
@pytest.mark.parametrize("return_config", [False, True])
def test_bake_each_launch_receives_fresh_duration(
    client: DockerClient,
    mocker: MockerFixture,
    call: buildx.BuildxCall | None,
    return_config: bool,
) -> None:
    commands = mocker.patch.object(buildx, "run", return_value='{"target":{}}')

    client.buildx.bake(
        "app",
        call=call,
        return_config=return_config,
        variables={"TAG": "value"},
        timeout=2.5,
    )

    normal_build = call in (None, "build")
    assert commands.call_count == (2 if return_config and normal_build else 1)
    for launched in commands.call_args_list:
        assert launched.kwargs["timeout"] == 2.5
        assert launched.kwargs["env"] == {"TAG": "value"}
    if commands.call_count == 2:
        assert "--print" not in commands.call_args_list[0].args[0]
        assert "--print" in commands.call_args_list[1].args[0]


def test_bake_expired_build_skips_config_launch(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(
        buildx, "run", side_effect=DockerTimeout(["docker-test"], -9)
    )

    with pytest.raises(DockerTimeout):
        client.buildx.bake(timeout=2.5)

    commands.assert_called_once()


def test_buildx_stream_adapter_forwards_environment_and_deadline(
    mocker: MockerFixture,
) -> None:
    started = Mock()
    started.__iter__ = Mock(return_value=iter([("stderr", b"log\xff\n")]))
    launch = mocker.patch.object(
        buildx, "stream_stdout_and_stderr", return_value=started
    )
    variables: Mapping[str, str] = {"TAG": "value"}

    logs = buildx.stream_buildx_logs(
        ["docker-test", "buildx", "bake"], variables, timeout=2.5
    )

    launch.assert_called_once_with(
        ["docker-test", "buildx", "bake"], env=variables, timeout=2.5
    )
    assert next(logs) == "log\ufffd\n"
    logs.close()
    started.close.assert_called_once()


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("operation", ["container", "root", "compose", "service"])
def test_log_deadline_forwarding_keeps_inspection_unbudgeted(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["container", "root", "compose", "service"],
    stream: bool,
    timeout: float | None,
) -> None:
    module = {
        "container": container,
        "root": container,
        "compose": compose,
        "service": service,
    }[operation]
    inspect = mocker.patch.object(
        module, "run", return_value='[{"Id":"workload","ID":"workload"}]'
    )
    raw = mocker.MagicMock()
    raw.__iter__.return_value = iter([("stdout", b"app | log\n")])
    launch = mocker.patch.object(module, "stream_stdout_and_stderr", return_value=raw)
    client.client_config.compose_project_name = "observation"

    match operation:
        case "container":
            result = client.container.logs(
                "workload", tail=3, follow=True, stream=stream, timeout=timeout
            )
        case "root":
            result = client.logs(
                "workload", tail=3, follow=True, stream=stream, timeout=timeout
            )
        case "compose":
            result = client.compose.logs(
                "workload", "3", True, stream=stream, timeout=timeout
            )
        case "service":
            result = client.service.logs(
                "workload", tail=3, follow=True, stream=stream, timeout=timeout
            )
        case unreachable:
            assert_never(unreachable)

    assert result is raw if stream else result == "app | log\n"
    group = "container" if operation == "root" else operation
    expected = ["docker-test", group]
    if operation == "compose":
        expected += ["--project-name", "observation"]
    expected += ["logs"]
    if operation == "compose":
        expected += ["--no-color"]
    expected += ["--tail", "3", "--follow", "workload"]
    assert [str(arg) for arg in launch.call_args.args[0]] == expected
    assert launch.call_args.kwargs == {"timeout": timeout}
    if operation == "compose":
        inspect.assert_not_called()
    else:
        inspect.assert_called_once_with(["docker-test", group, "inspect", "workload"])


def test_container_handle_forwards_log_deadline(
    client: DockerClient, mocker: MockerFixture
) -> None:
    handle = Container(client.client_config, "workload", is_immutable_id=True)
    logs = mocker.patch.object(container.ContainerCLI, "logs", return_value="log\n")

    assert handle.logs(tail=3, timeout=2.5) == "log\n"

    logs.assert_called_once_with(
        handle,
        details=False,
        since=None,
        tail=3,
        timestamps=False,
        until=None,
        timeout=2.5,
    )


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("operation", ["compose", "system"])
def test_event_deadline_forwarding_preserves_flags_and_mapping(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["compose", "system"],
    timeout: float | None,
) -> None:
    module = compose if operation == "compose" else system
    raw = mocker.MagicMock()
    raw.__iter__.return_value = iter(
        [
            ("stderr", b"not an event\n"),
            ("stdout", b'{"action":"start","Action":"start","service":"web"}\n'),
        ]
    )
    launch = mocker.patch.object(module, "stream_stdout_and_stderr", return_value=raw)
    client.client_config.compose_project_name = "observation"

    if operation == "compose":
        events = client.compose.events(["web"], timeout=timeout)
        expected = [
            "docker-test",
            "compose",
            "--project-name",
            "observation",
            "events",
            "--json",
            "web",
        ]
    else:
        events = client.system.events(filters={"container": "web"}, timeout=timeout)
        expected = [
            "docker-test",
            "system",
            "events",
            "--format",
            "{{json .}}",
            "--filter",
            "container=web",
        ]
    with events:
        result = list(events)

    assert [event.action for event in result] == ["start"]
    assert list(launch.call_args.args[0]) == expected
    assert launch.call_args.kwargs == {"timeout": timeout}


def test_subscription_deadlines_are_keyword_only_in_every_overload(
    client: DockerClient,
) -> None:
    for method in (
        container.Container.logs,
        client.container.logs,
        client.logs,
        client.compose.logs,
        client.service.logs,
        client.compose.events,
        client.system.events,
        client.compose.build,
        client.compose.execute,
        client.container.wait,
        client.wait,
    ):
        for variant in (method, *get_overloads(method)):
            parameter = signature(variant).parameters["timeout"]
            assert parameter.kind is Parameter.KEYWORD_ONLY
            assert parameter.default is None


def test_registry_transfer_deadlines_are_keyword_only_in_every_overload(
    client: DockerClient,
) -> None:
    plugin_handle = Plugin(client.client_config, "plugin", is_immutable_id=True)
    for method in (
        image.ImageCLI.pull,
        client.image.pull,
        client.pull,
        compose.ComposeCLI.pull,
        client.compose.pull,
        manifest.ManifestCLI.push,
        client.manifest.push,
        plugin.PluginCLI.push,
        client.plugin.push,
        plugin.Plugin.push,
        plugin_handle.push,
    ):
        for variant in (method, *get_overloads(method)):
            parameter = signature(variant).parameters["timeout"]
            assert parameter.kind is Parameter.KEYWORD_ONLY
            if variant is method:
                assert parameter.default is None
            else:
                assert parameter.default in (None, Ellipsis)


def test_image_pull_each_threadpool_worker_receives_the_selected_budget(
    client: DockerClient, mocker: MockerFixture
) -> None:
    def execute(args: Sequence[object], **kwargs: object) -> str:
        command = [str(arg) for arg in args]
        if command[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": "sha256:" + command[-1]}])
        assert command[1:3] == ["image", "pull"]
        assert kwargs["timeout"] == 2.5
        return ""

    commands = mocker.patch.object(image, "run", side_effect=execute)

    client.image.pull(["one", "two", "three"], quiet=True, timeout=2.5)

    pull_calls = [
        launched
        for launched in commands.call_args_list
        if [str(arg) for arg in launched.args[0]][1:3] == ["image", "pull"]
    ]
    assert len(pull_calls) == 3
    assert all(launched.kwargs["timeout"] == 2.5 for launched in pull_calls)


def test_manifest_push_preflight_is_outside_selected_budget(
    client: DockerClient, mocker: MockerFixture
) -> None:
    def execute(args: Sequence[object], **kwargs: object) -> str:
        command = [str(arg) for arg in args]
        if command[1:3] == ["manifest", "inspect"]:
            assert "timeout" not in kwargs
            return "{}"
        assert command[1:3] == ["manifest", "push"]
        assert kwargs["timeout"] == 2.5
        return ""

    commands = mocker.patch.object(manifest, "run", side_effect=execute)

    client.manifest.push("manifest-list", timeout=2.5)

    assert commands.call_count == 2


@pytest.mark.parametrize("stream", [False, True])
def test_compose_pull_forwards_selected_budget_to_both_modes(
    client: DockerClient, mocker: MockerFixture, stream: bool
) -> None:
    execute = mocker.patch.object(
        compose, "stream_stdout_and_stderr" if stream else "run", return_value="done"
    )

    client.compose.pull("app", quiet=not stream, stream_logs=stream, timeout=2.5)

    execute.assert_called_once()
    assert execute.call_args.kwargs["timeout"] == 2.5


def test_compose_pull_empty_services_skips_launch_regardless_of_timeout(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(compose, "run")

    assert client.compose.pull([], timeout=2.5) is None

    commands.assert_not_called()


@pytest.mark.parametrize("handle", [False, True])
def test_plugin_push_forwards_selected_budget(
    client: DockerClient, mocker: MockerFixture, handle: bool
) -> None:
    commands = mocker.patch.object(plugin, "run", return_value="")

    if handle:
        Plugin(client.client_config, "my-plugin", is_immutable_id=True).push(
            timeout=2.5
        )
    else:
        client.plugin.push("my-plugin", timeout=2.5)

    commands.assert_called_once()
    assert commands.call_args.kwargs["timeout"] == 2.5


def _scoped_default(config: ClientConfig) -> float | None:
    return get_execution_policy([PolicyExecutable("docker", config)]).default_timeout


def test_execution_timeout_overrides_default_without_mutating_config() -> None:
    config = ClientConfig(default_timeout=9.0)

    with execution_timeout(config, 2.5):
        assert _scoped_default(config) == 2.5

    assert _scoped_default(config) == 9.0
    assert config.default_timeout == 9.0


def test_execution_timeout_innermost_scope_wins_and_outer_returns() -> None:
    config = ClientConfig()

    with execution_timeout(config, 5.0):
        with execution_timeout(config, 1.5):
            assert _scoped_default(config) == 1.5
        assert _scoped_default(config) == 5.0

    assert _scoped_default(config) is None


def test_execution_timeout_none_binds_nothing_and_keeps_outer_scope() -> None:
    config = ClientConfig(default_timeout=9.0)

    with execution_timeout(config, None):
        assert _scoped_default(config) == 9.0
        with execution_timeout(config, 4.0), execution_timeout(config, None):
            assert _scoped_default(config) == 4.0


def test_execution_timeout_matches_config_identity_not_equality() -> None:
    scoped, equal_twin = ClientConfig(), ClientConfig()
    assert scoped == equal_twin

    with execution_timeout(scoped, 2.5):
        assert _scoped_default(scoped) == 2.5
        assert _scoped_default(equal_twin) is None


def test_execution_timeout_scopes_for_different_configs_coexist() -> None:
    first, second = ClientConfig(), ClientConfig()

    with execution_timeout(first, 1.0), execution_timeout(second, 2.0):
        assert _scoped_default(first) == 1.0
        assert _scoped_default(second) == 2.0


def test_execution_timeout_restores_scope_when_body_raises() -> None:
    config = ClientConfig(default_timeout=9.0)

    with pytest.raises(KeyboardInterrupt), execution_timeout(config, 2.5):
        raise KeyboardInterrupt

    assert _scoped_default(config) == 9.0


def test_execution_timeout_bounds_implicit_launch_and_explicit_timeout_dominates() -> (
    None
):
    config = ClientConfig()
    child = [PolicyExecutable(sys.executable, config), "-c"]
    sleeper = [*child, "import time; time.sleep(0.5); print('done')"]

    with execution_timeout(config, 0.1):
        with pytest.raises(DockerTimeout):
            run(sleeper)
        assert run(sleeper, timeout=10.0) == "done"


def test_execution_timeout_is_isolated_between_concurrent_threads() -> None:
    config = ClientConfig(default_timeout=9.0)
    entered = threading.Barrier(2, timeout=10)
    seen: dict[str, float | None] = {}

    def worker(name: str, timeout: float) -> None:
        with execution_timeout(config, timeout):
            entered.wait()
            seen[name] = _scoped_default(config)
            entered.wait()

    threads = [
        threading.Thread(target=worker, args=("a", 1.0)),
        threading.Thread(target=worker, args=("b", 2.0)),
    ]
    with execution_timeout(config, 5.0):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert _scoped_default(config) == 5.0

    assert seen == {"a": 1.0, "b": 2.0}
    assert _scoped_default(config) == 9.0


def _inspect_reply(args: Sequence[object]) -> str:
    names = [str(arg) for arg in args][3:]
    return json.dumps([{"Id": "sha256:" + name} for name in names])


def _short_child(
    *_: object, timeout: float | None = None, **__: object
) -> ProcessStream:
    return stream_stdout_and_stderr(
        [sys.executable, "-c", "print('x')"], timeout=timeout
    )


def _is_push(args: Sequence[object]) -> bool:
    return [str(arg) for arg in args][1:3] == ["image", "push"]


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("names", [["one"], ["one", "two", "three"]])
def test_image_push_blocking_launches_receive_budget_and_inspect_does_not(
    client: DockerClient,
    mocker: MockerFixture,
    names: list[str],
    timeout: float | None,
) -> None:
    def execute(args: Sequence[object], **kwargs: object) -> str:
        if _is_push(args):
            assert kwargs["timeout"] == timeout
            return ""
        assert "timeout" not in kwargs
        return _inspect_reply(args)

    commands = mocker.patch.object(image, "run", side_effect=execute)

    assert client.image.push(names, quiet=True, timeout=timeout) is None

    pushes = [call for call in commands.call_args_list if _is_push(call.args[0])]
    assert len(pushes) == len(names)
    assert len(commands.call_args_list) == len(names) + 1


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("names", [["one"], ["one", "two", "three"]])
def test_image_push_stream_children_receive_budget_and_inspect_does_not(
    client: DockerClient,
    mocker: MockerFixture,
    names: list[str],
    timeout: float | None,
) -> None:
    def inspect(args: Sequence[object], **kwargs: object) -> str:
        assert "timeout" not in kwargs
        return _inspect_reply(args)

    mocker.patch.object(image, "run", side_effect=inspect)
    launch = mocker.patch.object(
        image, "stream_stdout_and_stderr", side_effect=_short_child
    )

    stream = client.image.push(names, stream_logs=True, timeout=timeout)
    assert stream is not None
    with stream:
        assert sorted(name for name, _ in stream) == sorted(names)

    assert [call.kwargs["timeout"] for call in launch.call_args_list] == [
        timeout
    ] * len(names)


def test_multi_image_stream_defers_every_child_launch_to_first_consumption(
    client: DockerClient, mocker: MockerFixture
) -> None:
    mocker.patch.object(
        image, "run", side_effect=lambda args, **_: _inspect_reply(args)
    )
    launch = mocker.patch.object(
        image, "stream_stdout_and_stderr", side_effect=_short_child
    )

    stream = client.image.push(["one", "two"], stream_logs=True, timeout=2.5)
    assert stream is not None

    launch.assert_not_called()
    with stream:
        list(stream)
    assert [call.kwargs["timeout"] for call in launch.call_args_list] == [2.5, 2.5]


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_image_push_validates_before_consuming_or_inspecting(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(image, "run")
    consumed: list[str] = []

    def references() -> Iterator[str]:
        consumed.append("read")
        yield "one"

    for argument in ([], "one", references()):
        with pytest.raises(ValueError):
            client.image.push(argument, timeout=timeout)

    assert consumed == []
    commands.assert_not_called()


def test_image_push_empty_request_returns_none_after_valid_timeout(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(image, "run")
    launch = mocker.patch.object(image, "stream_stdout_and_stderr")

    assert client.image.push([], timeout=2.5) is None
    assert client.push([], stream_logs=True, timeout=2.5) is None

    commands.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("mode", ["unattached", "captured", "streamed"])
@pytest.mark.parametrize("entry", ["cli", "root", "handle"])
def test_container_start_forwards_budget_for_every_mode_and_entry(
    client: DockerClient,
    mocker: MockerFixture,
    entry: Literal["cli", "root", "handle"],
    mode: Literal["unattached", "captured", "streamed"],
    timeout: float | None,
) -> None:
    streamed = mode == "streamed"
    execute = mocker.patch.object(
        container, "stream_stdout_and_stderr" if streamed else "run", return_value="out"
    )
    attach = mode != "unattached"
    handle = Container(client.client_config, "workload", is_immutable_id=True)

    match entry:
        case "cli":
            client.container.start("workload", attach, stream=streamed, timeout=timeout)
        case "root":
            client.start("workload", attach, stream=streamed, timeout=timeout)
        case "handle":
            handle.start(attach, stream=streamed, timeout=timeout)
        case unreachable:
            assert_never(unreachable)

    execute.assert_called_once()
    assert execute.call_args.kwargs == {"timeout": timeout}
    assert [str(arg) for arg in execute.call_args.args[0]][1:3] == [
        "container",
        "start",
    ]


def test_container_handle_start_forwards_positional_options_and_deadline(
    client: DockerClient, mocker: MockerFixture
) -> None:
    handle = Container(client.client_config, "workload", is_immutable_id=True)
    start = mocker.patch.object(container.ContainerCLI, "start", return_value="out")

    assert handle.start(True, True, False, "ctrl-x", timeout=2.5) == "out"

    start.assert_called_once_with(handle, True, True, False, "ctrl-x", timeout=2.5)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_container_start_validates_before_consuming_or_launching(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(container, "run")
    launch = mocker.patch.object(container, "stream_stdout_and_stderr")
    consumed: list[str] = []

    def references() -> Iterator[str]:
        consumed.append("read")
        yield "workload"

    for argument in ([], "workload", references()):
        with pytest.raises(ValueError):
            client.container.start(argument, timeout=timeout)
    handle = Container(client.client_config, "workload", is_immutable_id=True)
    with pytest.raises(ValueError):
        handle.start(timeout=timeout)

    assert consumed == []
    commands.assert_not_called()
    launch.assert_not_called()


def test_container_start_empty_request_returns_none_after_valid_timeout(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(container, "run")

    assert client.container.start([], timeout=2.5) is None

    commands.assert_not_called()


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("prune", [False, True])
def test_compose_watch_forwards_budget_and_keeps_explicit_prune(
    client: DockerClient, mocker: MockerFixture, prune: bool, timeout: float | None
) -> None:
    launch = mocker.patch.object(
        compose, "stream_stdout_and_stderr", return_value="stream"
    )

    assert client.compose.watch("web", prune=prune, timeout=timeout) == "stream"

    assert launch.call_args.kwargs == {"timeout": timeout}
    assert f"--prune={str(prune).lower()}" in [str(a) for a in launch.call_args.args[0]]


@pytest.mark.parametrize("timeout", [0.0, True, float("nan")])
def test_compose_watch_and_push_validate_before_empty_selection(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(compose, "run")
    launch = mocker.patch.object(compose, "stream_stdout_and_stderr")

    for call in (
        lambda: client.compose.watch([], timeout=timeout),
        lambda: client.compose.watch(timeout=timeout),
        lambda: client.compose.push([], timeout=timeout),
        lambda: client.compose.push(timeout=timeout),
    ):
        with pytest.raises(ValueError):
            call()

    commands.assert_not_called()
    launch.assert_not_called()


def test_compose_watch_and_push_empty_services_skip_launch_with_valid_timeout(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(compose, "run")
    launch = mocker.patch.object(compose, "stream_stdout_and_stderr")

    assert client.compose.watch([], timeout=2.5) is None
    assert client.compose.push([], timeout=2.5) is None

    commands.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("timeout", [None, 2.5])
def test_compose_push_forwards_budget(
    client: DockerClient, mocker: MockerFixture, timeout: float | None
) -> None:
    commands = mocker.patch.object(compose, "run", return_value="")

    assert client.compose.push(["web"], timeout=timeout) is None

    assert commands.call_args.kwargs == {"timeout": timeout}
    assert [str(arg) for arg in commands.call_args.args[0]][-2:] == ["push", "web"]


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("stream", [False, True])
def test_buildx_prune_forwards_budget_without_a_cli_timeout_flag(
    client: DockerClient, mocker: MockerFixture, stream: bool, timeout: float | None
) -> None:
    execute = mocker.patch.object(
        buildx, "stream_buildx_logs" if stream else "run", return_value="out"
    )

    client.buildx.prune(
        all=True, filters={"until": "1h"}, stream_logs=stream, timeout=timeout
    )

    execute.assert_called_once()
    assert execute.call_args.kwargs == {"timeout": timeout}
    argv = [str(arg) for arg in execute.call_args.args[0]]
    assert argv[1:] == [
        "buildx",
        "prune",
        "--force",
        "--all",
        "--filter",
        "until=1h",
    ]


@pytest.mark.parametrize("timeout", [0.0, True, float("inf")])
def test_buildx_prune_validates_before_filters_and_launch(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(buildx, "run")
    launch = mocker.patch.object(buildx, "stream_buildx_logs")
    consumed: list[str] = []

    def filters() -> Iterator[tuple[str, str]]:
        consumed.append("read")
        yield ("until", "1h")

    for stream in (False, True):
        with pytest.raises(ValueError):
            client.buildx.prune(filters=filters(), stream_logs=stream, timeout=timeout)

    assert consumed == []
    commands.assert_not_called()
    launch.assert_not_called()


def test_long_launch_deadlines_are_keyword_only_in_every_overload(
    client: DockerClient,
) -> None:
    handle = Container(client.client_config, "workload", is_immutable_id=True)
    for method in (
        image.ImageCLI.push,
        client.image.push,
        client.push,
        container.ContainerCLI.start,
        client.container.start,
        client.start,
        container.Container.start,
        handle.start,
        compose.ComposeCLI.watch,
        client.compose.watch,
        compose.ComposeCLI.push,
        client.compose.push,
        buildx.BuildxCLI.prune,
        client.buildx.prune,
    ):
        for variant in (method, *get_overloads(method)):
            parameter = signature(variant).parameters["timeout"]
            assert parameter.kind is Parameter.KEYWORD_ONLY
            assert parameter.default is None or (
                variant is not method and parameter.default is Ellipsis
            )


def test_long_launch_aliases_expose_the_same_signatures(client: DockerClient) -> None:
    assert signature(client.push) == signature(client.image.push)
    assert signature(client.start) == signature(client.container.start)
    assert client.push.__func__ is image.ImageCLI.push
    assert client.start.__func__ is container.ContainerCLI.start


def _service_inspect(args: Sequence[object], **kwargs: object) -> str:
    assert "timeout" not in kwargs
    return json.dumps([{"ID": "workload", "Spec": {"Name": "workload"}}])


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("operation", ["create", "update", "rollback", "scale"])
def test_service_calls_forward_budget_and_keep_scale_preflight_unbudgeted(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["create", "update", "rollback", "scale"],
    timeout: float | None,
) -> None:
    def execute(args: Sequence[object], **kwargs: object) -> str:
        if [str(arg) for arg in args][1:3] == ["service", "inspect"]:
            return _service_inspect(args, **kwargs)
        assert kwargs["timeout"] == timeout
        return "service-id"

    commands = mocker.patch.object(service, "run", side_effect=execute)

    match operation:
        case "create":
            client.service.create("app", None, timeout=timeout)
        case "update":
            client.service.update("workload", timeout=timeout)
        case "rollback":
            client.service.rollback("workload", timeout=timeout)
        case "scale":
            client.service.scale({"workload": 2}, timeout=timeout)
        case unreachable:
            assert_never(unreachable)

    launched = [call for call in commands.call_args_list if "timeout" in call.kwargs]
    assert len(launched) == 1
    assert [str(arg) for arg in launched[0].args[0]][1:3] == ["service", operation]
    assert len(commands.call_args_list) == (2 if operation == "scale" else 1)


@pytest.mark.parametrize("method", ["update", "scale"])
def test_service_handle_forwards_budget_to_the_cli(
    client: DockerClient, mocker: MockerFixture, method: Literal["update", "scale"]
) -> None:
    handle = Service(client.client_config, "workload", is_immutable_id=True)
    forwarded = mocker.patch.object(service.ServiceCLI, method)

    if method == "update":
        handle.update(force=True, timeout=2.5)
    else:
        handle.scale(3, True, timeout=2.5)

    forwarded.assert_called_once()
    assert forwarded.call_args.kwargs["timeout"] == 2.5
    assert forwarded.call_args.args[0] == (
        handle if method == "update" else {handle: 3}
    )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_service_calls_validate_before_lookup_inspect_and_policy_options(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(service, "run")
    lookup = mocker.patch.object(
        DockerCLICaller, "docker_cmd", new_callable=PropertyMock
    )
    options = mocker.patch.object(service, "update_options")
    handle = Service(client.client_config, "workload", is_immutable_id=True)

    for call in (
        lambda: client.service.create("app", None, timeout=timeout),
        lambda: client.service.update("workload", timeout=timeout),
        lambda: client.service.rollback("workload", timeout=timeout),
        lambda: client.service.scale({}, timeout=timeout),
        lambda: client.service.scale({"workload": 2}, timeout=timeout),
        lambda: handle.update(timeout=timeout),
        lambda: handle.scale(2, timeout=timeout),
    ):
        with pytest.raises(ValueError):
            call()

    commands.assert_not_called()
    lookup.assert_not_called()
    options.assert_not_called()


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("detach", [False, True])
def test_swarm_ca_forwards_the_same_budget_to_each_launch(
    client: DockerClient, mocker: MockerFixture, detach: bool, timeout: float | None
) -> None:
    commands = mocker.patch.object(swarm, "run", return_value="certificate")

    result = client.swarm.ca(rotate=True, detach=detach, timeout=timeout)

    assert result == (None if detach else "certificate")
    assert len(commands.call_args_list) == (1 if detach else 2)
    assert all(call.kwargs == {"timeout": timeout} for call in commands.call_args_list)


def test_swarm_ca_first_expiry_skips_the_certificate_read(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(
        swarm, "run", side_effect=DockerTimeout(["docker-test"], -9)
    )

    with pytest.raises(DockerTimeout):
        client.swarm.ca(rotate=True, timeout=2.5)

    commands.assert_called_once()


@pytest.mark.parametrize("timeout", [None, 2.5])
def test_swarm_join_forwards_budget(
    client: DockerClient, mocker: MockerFixture, timeout: float | None
) -> None:
    commands = mocker.patch.object(swarm, "run", return_value="")

    client.swarm.join("10.0.0.1:2377", token="secret", timeout=timeout)

    assert commands.call_args.kwargs == {"timeout": timeout}
    assert [str(arg) for arg in commands.call_args.args[0]][1:3] == ["swarm", "join"]


@pytest.mark.parametrize("timeout", [None, 2.5])
def test_stack_deploy_forwards_budget_with_environment(
    client: DockerClient, mocker: MockerFixture, timeout: float | None
) -> None:
    commands = mocker.patch.object(stack, "run", return_value="")

    deployed = client.stack.deploy("web", variables={"TAG": "v1"}, timeout=timeout)

    assert deployed.name == "web"
    assert commands.call_args.kwargs == {
        "capture_stdout": False,
        "env": {"TAG": "v1"},
        "timeout": timeout,
    }


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_stack_deploy_validates_before_command_lookup_and_env_files(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(stack, "run")
    read = mocker.patch.object(stack, "read_env_files")
    lookup = mocker.patch.object(
        DockerCLICaller, "docker_cmd", new_callable=PropertyMock
    )

    with pytest.raises(ValueError):
        client.stack.deploy("web", env_files=[Path("a.env")], timeout=timeout)

    read.assert_not_called()
    lookup.assert_not_called()
    commands.assert_not_called()


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("operation", ["install", "upgrade", "handle_upgrade"])
def test_plugin_install_and_upgrade_forward_budget_not_a_docker_flag(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["install", "upgrade", "handle_upgrade"],
    timeout: float | None,
) -> None:
    def execute(args: Sequence[object], **kwargs: object) -> str:
        if [str(arg) for arg in args][1:3] == ["plugin", "inspect"]:
            assert "timeout" not in kwargs
            return '[{"Id":"plugin-id","Name":"my-plugin"}]'
        return ""

    commands = mocker.patch.object(plugin, "run", side_effect=execute)

    match operation:
        case "install":
            client.plugin.install("my-plugin", timeout=timeout)
        case "upgrade":
            client.plugin.upgrade("my-plugin", timeout=timeout)
        case "handle_upgrade":
            Plugin(client.client_config, "my-plugin", is_immutable_id=True).upgrade(
                timeout=timeout
            )
        case unreachable:
            assert_never(unreachable)

    launched = [call for call in commands.call_args_list if "timeout" in call.kwargs]
    assert len(launched) == 1
    assert launched[0].kwargs == {"timeout": timeout}
    assert not any("timeout" in str(arg) for arg in launched[0].args[0])
    assert len(commands.call_args_list) == (2 if operation == "install" else 1)


def test_plugin_handle_upgrade_forwards_positional_options_and_budget(
    client: DockerClient, mocker: MockerFixture
) -> None:
    handle = Plugin(client.client_config, "my-plugin", is_immutable_id=True)
    upgrade = mocker.patch.object(plugin.PluginCLI, "upgrade")

    handle.upgrade("remote", False, True, timeout=2.5)

    upgrade.assert_called_once_with(handle, "remote", False, True, timeout=2.5)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_plugin_install_and_upgrade_validate_before_command_lookup(
    client: DockerClient, mocker: MockerFixture, timeout: float
) -> None:
    commands = mocker.patch.object(plugin, "run")
    lookup = mocker.patch.object(
        DockerCLICaller, "docker_cmd", new_callable=PropertyMock
    )
    handle = Plugin(client.client_config, "my-plugin", is_immutable_id=True)

    for call in (
        lambda: client.plugin.install("my-plugin", timeout=timeout),
        lambda: client.plugin.upgrade("my-plugin", timeout=timeout),
        lambda: handle.upgrade(timeout=timeout),
    ):
        with pytest.raises(ValueError):
            call()

    commands.assert_not_called()
    lookup.assert_not_called()


def test_plugin_enable_timeout_stays_docker_option_without_local_deadline(
    client: DockerClient, mocker: MockerFixture
) -> None:
    commands = mocker.patch.object(plugin, "run", return_value="")

    client.plugin.enable("my-plugin", timeout=7)
    Plugin(client.client_config, "my-plugin", is_immutable_id=True).enable(timeout=9)

    first, second = commands.call_args_list
    assert [str(arg) for arg in first.args[0]][1:] == [
        "plugin",
        "enable",
        "--timeout",
        "7",
        "my-plugin",
    ]
    assert [str(arg) for arg in second.args[0]][-3:-1] == ["--timeout", "9"]
    assert first.kwargs == second.kwargs == {}


def test_swarm_and_plugin_deadlines_are_keyword_only_and_append_to_old_signatures(
    client: DockerClient,
) -> None:
    handle = Service(client.client_config, "workload", is_immutable_id=True)
    plugin_handle = Plugin(client.client_config, "plug", is_immutable_id=True)
    for method in (
        service.ServiceCLI.create,
        client.service.create,
        service.ServiceCLI.update,
        client.service.update,
        service.ServiceCLI.rollback,
        client.service.rollback,
        service.ServiceCLI.scale,
        client.service.scale,
        service.Service.update,
        handle.update,
        service.Service.scale,
        handle.scale,
        swarm.SwarmCLI.ca,
        client.swarm.ca,
        swarm.SwarmCLI.join,
        client.swarm.join,
        stack.StackCLI.deploy,
        client.stack.deploy,
        plugin.PluginCLI.install,
        client.plugin.install,
        plugin.PluginCLI.upgrade,
        client.plugin.upgrade,
        plugin.Plugin.upgrade,
        plugin_handle.upgrade,
    ):
        parameter = signature(method).parameters["timeout"]
        assert parameter.kind is Parameter.KEYWORD_ONLY
        assert parameter.default is None


@pytest.mark.parametrize(
    "method",
    [
        service.ServiceCLI.create,
        service.ServiceCLI.scale,
        swarm.SwarmCLI.ca,
        swarm.SwarmCLI.join,
        stack.StackCLI.deploy,
        plugin.PluginCLI.install,
        plugin.PluginCLI.upgrade,
        plugin.Plugin.upgrade,
        service.Service.scale,
    ],
)
def test_added_deadline_keeps_every_earlier_parameter_positional(
    method: Callable[..., object],
) -> None:
    parameters = list(signature(method).parameters.values())

    assert parameters[-1].name == "timeout"
    assert all(
        parameter.kind in (Parameter.POSITIONAL_OR_KEYWORD,)
        for parameter in parameters[:-1]
        if parameter.name != "self"
    )


def test_plugin_enable_has_no_local_deadline_parameter(client: DockerClient) -> None:
    for method in (plugin.PluginCLI.enable, plugin.Plugin.enable):
        parameter = signature(method).parameters["timeout"]
        assert parameter.kind is Parameter.POSITIONAL_OR_KEYWORD
