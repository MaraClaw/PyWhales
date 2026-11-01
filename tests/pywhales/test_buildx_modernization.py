"""Buildx option and result contracts with simulated commands and local children.

The commands fixture replaces subprocess responses and rejects process launches.
Streaming ownership cases instead start real Python children, not Docker builds.
"""

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal, assert_never
from unittest.mock import Mock, PropertyMock

import pytest
from pytest_mock import MockerFixture

from pywhales import DockerClient, Image
from pywhales.components.buildx import cli_wrapper as buildx
from pywhales.exceptions import DockerException
from pywhales.utils import ProcessStream, stream_stdout_and_stderr


@pytest.fixture
def client() -> DockerClient:
    result = DockerClient(context="build-context", host="build-host", tls=True)
    result.client_config._client_call_with_path = ["docker-test"]
    return result


@pytest.fixture
def commands(mocker: MockerFixture) -> Mock:
    """Record simulated Buildx requests and reject real process launches.

    Known inspection, build, and bake commands return fixture bytes. Simulated
    builds write requested image-ID files; pytest restores all patches afterward.

    Args:
        mocker: Installs the subprocess.run replacement and Popen rejection.

    Returns:
        The mutable subprocess.run mock, with requests in call_args_list.
    """

    def execute(
        args: list[str],
        *,
        input: bytes | None,
        stdout: int | None,
        stderr: int | None,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> CompletedProcess[bytes]:
        match args[args.index("buildx") + 1 :] if "buildx" in args else args[-3:]:
            case ["inspect", *_]:
                output = "Name: selected\n"
            case ["ls", *_]:
                output = json.dumps({"Name": "selected", "Driver": "docker"})
            case ["build", *_]:
                if "--iidfile" in args:
                    Path(args[args.index("--iidfile") + 1]).write_text("sha256:built")
                output = "report text\n"
            case ["bake", *_]:
                output = '{"target":{"app":{"context":"."}}}'
            case ["image", "inspect", _]:
                output = '[{"Id":"sha256:built"}]'
            case _:
                pytest.fail(f"Unexpected subprocess: {args}")
        return CompletedProcess(args, 0, output.encode(), b"")

    mocker.patch("subprocess.Popen", side_effect=AssertionError("unexpected process"))
    result = Mock(side_effect=execute)
    mocker.patch("subprocess.run", new=result)
    return result


@pytest.mark.parametrize("entrypoint", ["build", "image.build", "buildx.build"])
def test_modern_build_entrypoints_execute_buildx(
    client: DockerClient,
    commands: Mock,
    entrypoint: Literal["build", "image.build", "buildx.build"],
) -> None:
    match entrypoint:
        case "build":
            build = client.build
        case "image.build":
            build = client.image.build
        case "buildx.build":
            build = client.buildx.build
        case unreachable:
            assert_never(unreachable)

    result = build(
        "context",
        build_args={"VERSION": "3"},
        file=Path("Containerfile"),
        load=True,
        network="none",
        progress="plain",
        tags="app:local",
    )

    assert isinstance(result, Image)
    assert result.id == "sha256:built"
    assert result.client_config is client.client_config
    builds = [
        call.args[0] for call in commands.call_args_list if "build" in call.args[0]
    ]
    assert len(builds) == 1
    assert builds[0][:-3] == [
        "docker-test",
        "--context",
        "build-context",
        "--host",
        "build-host",
        "--tls",
        "buildx",
        "build",
        "--progress",
        "plain",
        "--build-arg",
        "VERSION",
        "--load",
        "--file",
        "Containerfile",
        "--network",
        "none",
        "--tag",
        "app:local",
    ]
    assert builds[0][-3] == "--iidfile"
    assert builds[0][-1] == "context"
    build_call = next(
        call for call in commands.call_args_list if "build" in call.args[0]
    )
    assert build_call.kwargs["env"]["VERSION"] == "3"


@pytest.mark.parametrize("call", [None, "build", "check", "outline", "targets"])
def test_bake_rejects_print_stream_before_execution(
    client: DockerClient, commands: Mock, call: buildx.BuildxCall | None
) -> None:
    with pytest.raises(ValueError, match="stream"):
        client.buildx.bake(print=True, stream_logs=True, call=call)
    commands.assert_not_called()


@pytest.mark.parametrize(("only_print", "expected"), [(False, 2), (True, 1)])
def test_bake_config_command_counts(
    client: DockerClient, commands: Mock, only_print: bool, expected: int
) -> None:
    result = client.buildx.bake("app", print=only_print)
    assert result == {"target": {"app": {"context": "."}}}
    assert commands.call_count == expected
    assert "--print" in commands.call_args.args[0]


def test_bake_without_config_uses_one_build(
    client: DockerClient, commands: Mock
) -> None:
    result = client.buildx.bake("app", return_config=False)
    assert result is None
    commands.assert_called_once()
    assert "--print" not in commands.call_args.args[0]


def test_build_resolves_driver_once_when_properties_expire(
    client: DockerClient, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(buildx.Builder, "_needs_reload", lambda self: True)
    result = client.build(".")
    assert isinstance(result, Image)
    assert result.client_config is client.client_config
    operations = [call.args[0] for call in commands.call_args_list]
    assert sum("buildx" in cmd and "inspect" in cmd for cmd in operations) == 1
    assert sum("buildx" in cmd and "ls" in cmd for cmd in operations) == 2
    assert len(operations) == 5
    assert all(cmd[:6] == client.client_config.docker_cmd for cmd in operations)


@pytest.mark.parametrize("call", ["check", "outline", "targets"])
@pytest.mark.parametrize("tags", [(), ("user:first", "user:second")])
def test_build_reports_bypass_image_recovery(
    client: DockerClient,
    commands: Mock,
    mocker: MockerFixture,
    call: buildx.BuildxReportCall,
    tags: tuple[str, ...],
) -> None:
    mocker.patch.object(
        buildx.tempfile,
        "TemporaryDirectory",
        side_effect=AssertionError("unexpected IIDfile"),
    )
    result = client.build("context", call=call, load=True, tags=tags)
    assert result == "report text"
    commands.assert_called_once()
    args = commands.call_args.args[0]
    assert args[-3:] == ["--call", call, "context"]
    assert "--iidfile" not in args
    assert args.count("--tag") == len(tags)


@pytest.mark.parametrize(
    ("filters", "annotations", "expected"),
    [
        (None, None, []),
        ([], [], []),
        (
            "deps",
            "key=value",
            ["--no-cache-filter", "deps", "--annotation", "key=value"],
        ),
        (
            ("deps", "build"),
            ("a=1", "b=2"),
            [
                "--no-cache-filter",
                "deps",
                "--no-cache-filter",
                "build",
                "--annotation",
                "a=1",
                "--annotation",
                "b=2",
            ],
        ),
    ],
)
def test_build_forwards_stage_filters_and_annotations(
    client: DockerClient,
    commands: Mock,
    filters: str | Sequence[str] | None,
    annotations: str | Sequence[str] | None,
    expected: list[str],
) -> None:
    client.build(".", push=True, no_cache_filter=filters, annotations=annotations)
    assert commands.call_args.args[0] == [
        "docker-test",
        "--context",
        "build-context",
        "--host",
        "build-host",
        "--tls",
        "buildx",
        "build",
        "--push",
        *expected,
        ".",
    ]
    commands.assert_called_once()


@pytest.mark.parametrize("call", [None, "build"])
@pytest.mark.parametrize(
    "output", [{}, {"type": "local"}, {"type": "docker", "dest": "image.tar"}]
)
def test_nonloading_builds_skip_inspection(
    client: DockerClient,
    commands: Mock,
    call: Literal["build"] | None,
    output: buildx.BuildxOptions,
) -> None:
    assert client.build(".", call=call, push=not output, output=output) is None
    commands.assert_called_once()
    assert "--iidfile" not in commands.call_args.args[0]


@pytest.mark.parametrize(
    ("driver", "load", "tags", "recovery"),
    [
        ("docker", False, (), "sha256:built"),
        ("docker", True, ("first", "second"), "sha256:built"),
        ("docker-container", False, ("first",), None),
        ("docker-container", True, (), None),
        ("docker-container", True, ("first", "second"), "first"),
    ],
)
def test_build_preserves_loading_and_tags(
    client: DockerClient,
    commands: Mock,
    mocker: MockerFixture,
    driver: str,
    load: bool,
    tags: tuple[str, ...],
    recovery: str | None,
) -> None:
    mocker.patch.object(
        buildx.Builder, "driver", new_callable=PropertyMock, return_value=driver
    )
    result = client.build(".", builder="selected", call="build", load=load, tags=tags)
    if recovery is None:
        assert result is None
    else:
        assert isinstance(result, Image)
        assert result.client_config is client.client_config
        assert commands.call_args.args[0][-3:] == ["image", "inspect", recovery]
    builds = [
        entry.args[0] for entry in commands.call_args_list if "build" in entry.args[0]
    ]
    assert len(builds) == 1
    assert builds[0].count("--tag") == len(tags)
    assert ("--iidfile" in builds[0]) == (recovery == "sha256:built")


def test_later_builds_resolve_the_driver_again(
    client: DockerClient, commands: Mock, mocker: MockerFixture
) -> None:
    driver = mocker.patch.object(
        buildx.Builder,
        "driver",
        new_callable=PropertyMock,
        side_effect=["docker", "docker-container"],
    )
    assert isinstance(client.build("."), Image)
    assert client.build(".") is None
    assert driver.call_count == 2
    assert (
        sum(
            "inspect" in entry.args[0] and "buildx" in entry.args[0]
            for entry in commands.call_args_list
        )
        == 2
    )


@pytest.mark.parametrize("driver", ["docker", "docker-container"])
def test_docker_output_returns_loaded_image(
    client: DockerClient, commands: Mock, mocker: MockerFixture, driver: str
) -> None:
    mocker.patch.object(
        buildx.Builder, "driver", new_callable=PropertyMock, return_value=driver
    )
    result = client.build(".", output={"type": "docker"}, tags="output:first")
    assert isinstance(result, Image)
    recovery = "sha256:built" if driver == "docker" else "output:first"
    assert commands.call_args.args[0][-3:] == ["image", "inspect", recovery]


@pytest.mark.parametrize("call", ["check", "outline", "targets"])
@pytest.mark.parametrize("config", [False, True])
def test_bake_reports_use_one_raw_text_call(
    client: DockerClient, commands: Mock, call: buildx.BuildxReportCall, config: bool
) -> None:
    commands.side_effect = None
    commands.return_value = CompletedProcess([], 0, b"Targets report\n", b"")
    result = client.buildx.bake("app", call=call, return_config=config)
    assert result == "Targets report"
    commands.assert_called_once()
    assert commands.call_args.args[0][-3:] == ["--call", call, "app"]
    assert "--print" not in commands.call_args.args[0]


@pytest.mark.parametrize("jobs", [None, 0, 2, "defer-error,parallel=2"])
@pytest.mark.parametrize("call", [None, "build"])
def test_bake_preserves_options_on_build_and_config_calls(
    client: DockerClient,
    commands: Mock,
    jobs: int | str | None,
    call: Literal["build"] | None,
) -> None:
    client.buildx.bake(
        "app", files="bake.hcl", variables={"TAG": "local"}, jobs=jobs, call=call
    )
    assert commands.call_count == 2
    first, second = [entry.args[0] for entry in commands.call_args_list]
    assert first == [value for value in second if value != "--print"]
    assert ("--call" in first) == (call is not None)
    assert ("--jobs" in first) == (jobs is not None)
    if jobs is not None:
        assert first[first.index("--jobs") + 1] == str(jobs)
    assert all(
        entry.kwargs["env"]["TAG"] == "local" for entry in commands.call_args_list
    )


@pytest.mark.parametrize("call", [None, "build", "check", "outline", "targets"])
def test_bake_rejects_incompatible_print_before_execution(
    client: DockerClient, commands: Mock, call: buildx.BuildxCall | None
) -> None:
    with pytest.raises(ValueError):
        client.buildx.bake(
            print=True, call=call, return_config=call is not None and call != "build"
        )
    commands.assert_not_called()


@pytest.mark.parametrize("operation", ["build", "bake"])
def test_report_errors_propagate_without_followup_calls(
    client: DockerClient, commands: Mock, operation: Literal["build", "bake"]
) -> None:
    commands.side_effect = None
    commands.return_value = CompletedProcess([], 7, b"", b"invalid frontend\n")
    with pytest.raises(DockerException) as raised:
        match operation:
            case "build":
                client.build(".", call="check")
            case "bake":
                client.buildx.bake(call="check")
            case unreachable:
                assert_never(unreachable)
    assert raised.value.return_code == 7
    commands.assert_called_once()


@pytest.mark.parametrize("operation", ["build", "bake"])
@pytest.mark.parametrize("call", [None, "build", "check", "outline", "targets"])
def test_streams_keep_process_ownership_and_skip_followups(
    client: DockerClient,
    mocker: MockerFixture,
    operation: Literal["build", "bake"],
    call: buildx.BuildxCall | None,
) -> None:
    def start(
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> ProcessStream:
        return stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                "import os, time; print(os.getenv('TAG', 'build'), flush=True); time.sleep(60)",
            ],
            env=env,
            timeout=timeout,
        )

    blocked = mocker.patch(
        "subprocess.run", side_effect=AssertionError("unexpected subprocess")
    )
    started = mocker.patch.object(buildx, "stream_stdout_and_stderr", side_effect=start)
    match operation:
        case "build":
            stream = client.build(".", stream_logs=True, call=call)
        case "bake":
            stream = client.buildx.bake(
                "app",
                stream_logs=True,
                variables={"TAG": "bake"},
                call=call,
                return_config=False,
            )
        case unreachable:
            assert_never(unreachable)
    with stream:
        assert next(stream) == operation + "\n"
    assert stream._stream._process.poll() is not None
    assert all(not thread.is_alive() for thread in stream._stream._threads)
    started.assert_called_once()
    blocked.assert_not_called()
