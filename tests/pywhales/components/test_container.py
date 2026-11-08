import contextlib
import inspect
import json
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from types import TracebackType
from unittest.mock import Mock, patch

import pytest
from pytest_mock import MockerFixture

import pywhales
from pywhales import DockerClient, Image, docker
from pywhales.client_config import ParsingError
from pywhales.components.container.cli_wrapper import ContainerStats
from pywhales.components.container.models import (
    ContainerInspectResult,
    ContainerState,
)
from pywhales.components.top import ContainerTopResult, TopParseError
from pywhales.exceptions import (
    DockerException,
    NoSuchContainer,
    NoSuchImage,
    NoSuchNetwork,
    NoSuchVolume,
)
from pywhales.test_utils import get_all_jsons, random_name
from pywhales.utils import stream_stdout_and_stderr
from tests.pywhales.components.hydration_fakes import (
    FakeCli,
    fake_daemon,
    identity_of,
)


@patch("pywhales.components.container.cli_wrapper.run")
@pytest.mark.parametrize(
    ("ps_args", "output", "headings", "rows"),
    [
        (
            [],
            "UID PID PPID C STIME TTY TIME CMD\nroot 7 1 0 12:00 ? 00:00:00 python -c print('two  spaces')",
            ("UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"),
            (
                (
                    "root",
                    "7",
                    "1",
                    "0",
                    "12:00",
                    "?",
                    "00:00:00",
                    "python -c print('two  spaces')",
                ),
            ),
        ),
        (
            ["-eo", "pid,args"],
            "PID COMMAND\n7 python  worker.py --label two words",
            ("PID", "COMMAND"),
            (("7", "python  worker.py --label two words"),),
        ),
        (
            ["--format=pid,args=Invocation"],
            "PID Invocation\n7 sleep 300",
            ("PID", "Invocation"),
            (("7", "sleep 300"),),
        ),
        (["-o", "pid", "-o", "ppid"], "PID PPID\n7 1", ("PID", "PPID"), (("7", "1"),)),
        (["-opid"], "PID", ("PID",), ()),
        (
            ["-owchan", "-o", "pid"],
            "WCHAN PID\ndo_wait 7",
            ("WCHAN", "PID"),
            (("do_wait", "7"),),
        ),
        (
            ["-C", "python"],
            "PID CMD\n7 python worker.py",
            ("PID", "CMD"),
            (("7", "python worker.py"),),
        ),
        (
            ["-Cpython"],
            "PID CMD\n7 python worker.py",
            ("PID", "CMD"),
            (("7", "python worker.py"),),
        ),
        (
            ["-o", "args"],
            "COMMAND\npython -c 'print(1)'",
            ("COMMAND",),
            (("python -c 'print(1)'",),),
        ),
    ],
)
def test_top_preserves_cells_when_docker_selects_columns(
    run_mock: Mock,
    ps_args: list[str],
    output: str,
    headings: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> None:
    client = DockerClient(client_call=[sys.executable, "engine"], host="ssh://host")
    run_mock.return_value = output

    result = client.container.top("workload", ps_args)

    assert isinstance(result, ContainerTopResult)
    assert (result.headings, result.rows, result.raw_output) == (headings, rows, output)
    run_mock.assert_called_once_with(
        [
            sys.executable,
            "engine",
            "--host",
            "ssh://host",
            "container",
            "top",
            "workload",
            *ps_args,
        ]
    )


@patch("pywhales.components.container.cli_wrapper.run")
@pytest.mark.parametrize(
    ("ps_args", "output"),
    [
        (["-o", "args=Invocation,pid"], "Invocation PID\nsleep 300 7"),
        (
            ["-o", "lstart,pid,args"],
            "STARTED PID COMMAND\nSun Oct 25 12:00:00 2026 7 sleep 300",
        ),
        (["-o", "pid=,args="], "7 sleep 300"),
        (["-o", "pid,unknown"], "PID OTHER\n7 value"),
        (["-eOargs=Invocation"], "PID Invocation CMD\n7 sleep 300 sleep 300"),
        (["-eh"], "7 sleep 300"),
        ([], "UID PID CMD\nroot 7"),
        ([], "CMD PID\nsleep 300 7"),
        ([], "PID PPID\n7 1 extra"),
        ([], ""),
    ],
)
def test_top_rejects_ambiguous_output_when_parsing_is_requested(
    run_mock: Mock, ps_args: list[str], output: str
) -> None:
    client = DockerClient(client_call=[sys.executable])
    run_mock.return_value = output

    with pytest.raises(TopParseError, match="raw=True"):
        client.container.top("workload", ps_args)


@patch(
    "pywhales.components.container.cli_wrapper.run",
    return_value="Invocation PID\nsleep 300 7\n",
)
def test_top_returns_raw_text_when_selection_is_ambiguous(run_mock: Mock) -> None:
    client = DockerClient(client_call=[sys.executable])

    output = client.container.top("workload", ["-o", "args=Invocation,pid"], raw=True)

    assert output == "Invocation PID\nsleep 300 7\n"


@patch("pywhales.utils.subprocess.run")
@pytest.mark.parametrize("raw", [False, True])
def test_top_retains_missing_container_exception_when_cli_fails(
    run_mock: Mock, raw: bool
) -> None:
    client = DockerClient(client_call=[sys.executable])
    run_mock.return_value = subprocess.CompletedProcess(
        [], 1, b"", b"Error: No such container: absent"
    )

    with pytest.raises(NoSuchContainer) as error:
        client.container.top("absent", raw=raw)

    assert error.value.return_code == 1
    assert error.value.docker_command[-3:] == ["container", "top", "absent"]


@pytest.mark.parametrize(
    "ps_args", [["-eo", "pid,args"], ["-o", "pid,args=Invocation"]]
)
def test_top_preserves_custom_command_when_inspecting_live_container(
    docker_client: DockerClient, ps_args: list[str]
) -> None:
    container = docker_client.run(
        "python:3.14.10-slim-trixie", ["sleep", "300"], detach=True
    )
    try:
        result = docker_client.container.top(container, ps_args)

        assert len(result.headings) == 2
        assert result.rows
        assert all(len(row) == 2 for row in result.rows)
        assert any(row[-1] == "sleep 300" for row in result.rows)
    finally:
        docker_client.remove(container, force=True)


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_hydrates_in_request_order(component: str) -> None:
    client = DockerClient(host="test-host")
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    references = ["second", "first", "second"]
    snapshots = [{"Id": name, "Name": name} for name in references]
    response = CompletedProcess([], 0, json.dumps(snapshots).encode(), b"")
    with (
        patch("subprocess.run", return_value=response) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = api.inspect(iter(references))
        assert [str(resource) for resource in resources] == references
        assert all(
            resource.client_config is client.client_config for resource in resources
        )
        assert all(
            resource._get_inspect_result() is resource._inspect_result
            for resource in resources
        )
        assert resources[0] is not resources[2]
        command.assert_called_once()
        assert command.call_args.args[0] == [
            "docker-test",
            "--host",
            "test-host",
            component,
            "inspect",
            *references,
        ]


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_empty_avoids_executable_lookup(component: str) -> None:
    client = DockerClient(client_call=["not-installed"])
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    with patch("shutil.which", side_effect=AssertionError("executable lookup")):
        assert api.inspect(iter(())) == []


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
@pytest.mark.parametrize(
    "payload",
    [
        "invalid json",
        "{}",
        "[]",
        "[{}, {}]",
        "[null, null]",
        '[{"Id":"first","Name":"first"}]',
        '[{"Id":"first","Name":"first"},{"Id":"second","Name":"second"},{}]',
        '[{"Id":"wrong","Name":"wrong"},{"Id":"second","Name":"second"}]',
        '[{"Id":"second","Name":"second"},{"Id":"first","Name":"first"}]',
        '[{"Id":"","Name":""},{"Id":"second","Name":"second"}]',
        '[{"Id":"first","Name":"first","Created":"invalid"},{"Id":"second","Name":"second"}]',
    ],
    ids=[
        "json",
        "shape",
        "empty",
        "identity",
        "item-shape",
        "short",
        "long",
        "mismatch",
        "reordered",
        "empty-identity",
        "model",
    ],
)
def test_batch_inspect_rejects_invalid_success(component: str, payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    if component == "volume":
        payload = payload.replace('"Created"', '"CreatedAt"')
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        pytest.raises(ParsingError),
    ):
        api.inspect(["first", "second"])


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_preserves_partial_command_failure(component: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    output = b'[{"Id":"first","Name":"first"}]'
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 17, output, b"permission denied"),
        ),
        pytest.raises(DockerException) as failure,
    ):
        api.inspect(["first", "second"])
    assert failure.value.return_code == 17
    assert failure.value.stdout == output.decode()
    assert failure.value.stderr == "permission denied"
    assert failure.value.docker_command == [
        "docker-test",
        component,
        "inspect",
        "first",
        "second",
    ]


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_preserves_scalar_and_lazy_list_contracts(component: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    resource_type = {
        "container": pywhales.Container,
        "image": pywhales.Image,
        "network": pywhales.Network,
        "volume": pywhales.Volume,
    }[component]
    snapshot = b'[{"Id":"identity","Name":"named"}]'
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, snapshot, b"")
    ) as command:
        resource = api.inspect("named")
        assert isinstance(resource, resource_type)
        assert str(resource) == ("named" if component == "volume" else "identity")
        assert resource.client_config is client.client_config
        assert resource._inspect_result is not None
        assert command.call_count == 1
        command.return_value = CompletedProcess([], 0, b"identity", b"")
        listed = api.list()
        assert len(listed) == 1
        assert str(listed[0]) == "identity"
        assert listed[0]._inspect_result is None
        assert command.call_count == 2


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_rejects_inconsistent_repeated_references(component: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    snapshots = [
        {"Id": "a" * 64, "Name": "named", "RepoTags": ["named:latest"]},
        {
            "Id": "b" * 64,
            "Name": "other" if component == "volume" else "named",
            "RepoTags": ["named:latest"],
        },
    ]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps(snapshots).encode(), b""),
        ),
        pytest.raises(ParsingError),
    ):
        api.inspect(["named", "named"])


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
def test_batch_inspect_cache_reload_and_expiration(component: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    snapshot = {"Id": "identity", "Name": "named", "RepoTags": ["named:latest"]}
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps([snapshot]).encode(), b""),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resource = api.inspect(["named"])[0]
        initial = resource._get_inspect_result()
        assert command.call_count == 1
        resource.reload()
        refreshed = resource._get_inspect_result()
        assert refreshed is not initial
        assert command.call_count == 2
        resource._refreshed_at = None
        assert resource._get_inspect_result() is not refreshed
        assert command.call_count == 3
        assert command.call_args.args[0] == [
            "docker-test",
            component,
            "inspect",
            "named" if component == "volume" else "identity",
        ]


def test_batch_inspect_container_aliases_and_reload() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    snapshot = {"Id": "a" * 64, "Name": "/named", "Path": "before"}
    existing = pywhales.Container(client.client_config, "a" * 64, is_immutable_id=True)

    def references() -> Iterator[str | pywhales.Container]:
        yield "named"
        yield existing
        yield "a" * 12

    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess(
                [], 0, json.dumps([snapshot] * 3).encode(), b""
            ),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = client.container.inspect(references())
        assert [resource.id for resource in resources] == ["a" * 64] * 3
        assert [resource.name for resource in resources] == ["named"] * 3
        assert resources[0].path == "before"
        assert command.call_count == 1
        snapshot["Path"] = "after"
        command.return_value = CompletedProcess(
            [], 0, json.dumps([snapshot]).encode(), b""
        )
        resources[0].reload()
        assert resources[0].path == "after"
        resources[0]._refreshed_at = None
        assert resources[0].path == "after"
        assert command.call_count == 3
        assert command.call_args.args[0][-1] == "a" * 64


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
@pytest.mark.parametrize("body_outcome", ["success", "error", "interrupt"])
@pytest.mark.parametrize(
    "cleanup_outcome", ["success", "missing", "error", "wrong-missing", "interrupt"]
)
def test_resource_cleanup_exception_matrix(
    component: str, body_outcome: str, cleanup_outcome: str
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    resource_type = {
        "container": pywhales.Container,
        "image": pywhales.Image,
        "network": pywhales.Network,
        "volume": pywhales.Volume,
    }[component]
    missing_type = {
        "container": NoSuchContainer,
        "image": NoSuchImage,
        "network": NoSuchNetwork,
        "volume": NoSuchVolume,
    }[component]
    resource = resource_type(client.client_config, "resource", is_immutable_id=True)
    body_error = {
        "success": None,
        "error": ValueError("body"),
        "interrupt": KeyboardInterrupt("body"),
    }[body_outcome]
    cleanup_error = {
        "success": None,
        "missing": missing_type(["docker-test", component, "rm", "resource"], 1),
        "error": DockerException(["docker-test", component, "rm", "resource"], 17),
        "wrong-missing": (NoSuchVolume if component != "volume" else NoSuchImage)(
            ["docker-test", component, "rm", "resource"], 1
        ),
        "interrupt": KeyboardInterrupt("cleanup"),
    }[cleanup_outcome]
    body_tb: TracebackType | None = None
    cleanup_tb: TracebackType | None = None

    def command_response(
        command: list[str], **kwargs: object
    ) -> CompletedProcess[bytes]:
        nonlocal cleanup_tb
        if command[2] == "inspect":
            return CompletedProcess(
                command,
                0,
                b'[{"Id":"resource","HostConfig":{"AutoRemove":false},"State":{"Running":false}}]',
                b"",
            )
        if cleanup_error is not None:
            try:
                raise cleanup_error
            except BaseException as failure:
                cleanup_tb = failure.__traceback__
                raise
        return CompletedProcess(command, 0, b"", b"")

    def use_resource() -> None:
        nonlocal body_tb
        with resource:
            if body_error is not None:
                try:
                    raise body_error
                except BaseException as failure:
                    body_tb = failure.__traceback__
                    raise

    with (
        patch("subprocess.run", side_effect=command_response) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        effective_cleanup = None if cleanup_outcome == "missing" else cleanup_error
        if body_error is None and effective_cleanup is None:
            use_resource()
        else:
            with pytest.raises(BaseException) as raised:
                use_resource()
            if body_error is not None and effective_cleanup is not None:
                assert isinstance(raised.value, BaseExceptionGroup)
                assert raised.value.exceptions == (body_error, effective_cleanup)
                assert body_error.__traceback__ is body_tb
            else:
                assert raised.value is (
                    body_error if body_error is not None else effective_cleanup
                )
            if effective_cleanup is not None:
                assert cleanup_tb is not None
                assert any(
                    frame is cleanup_tb.tb_frame
                    for frame, _ in traceback.walk_tb(effective_cleanup.__traceback__)
                )
        remove_command = command.call_args.args[0]
        operation = "rm" if component in {"container", "image"} else "remove"
        assert remove_command[:3] == ["docker-test", component, operation]
        assert remove_command[-1] == "resource"


@pytest.mark.parametrize("running", [False, True, None])
@pytest.mark.parametrize("auto_remove", [False, True, None])
@pytest.mark.parametrize("body_error_type", [None, ValueError])
def test_container_cleanup_preserves_stop_and_removal(
    running: bool | None,
    auto_remove: bool | None,
    body_error_type: type[ValueError] | None,
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    container = pywhales.Container(
        client.client_config, "resource", is_immutable_id=True
    )
    snapshot = {
        "Id": "resource",
        "HostConfig": {} if auto_remove is None else {"AutoRemove": auto_remove},
        "State": {} if running is None else {"Running": running},
    }
    body_error = body_error_type("body") if body_error_type is not None else None

    def use_container() -> None:
        with container:
            if body_error is not None:
                raise body_error

    with (
        patch(
            "subprocess.run",
            side_effect=[
                CompletedProcess([], 0, json.dumps([snapshot]).encode(), b""),
                CompletedProcess([], 0, b"resource", b""),
                CompletedProcess([], 0, b"resource", b""),
            ],
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        if body_error is None:
            use_container()
        else:
            with pytest.raises(ValueError) as raised:
                use_container()
            assert raised.value is body_error
    expected = [["docker-test", "container", "inspect", "resource"]]
    if running is True:
        expected.append(["docker-test", "container", "stop", "resource"])
    if auto_remove is not True:
        expected.append(["docker-test", "container", "rm", "--volumes", "resource"])
    assert [call.args[0] for call in command.call_args_list] == expected


@pytest.mark.parametrize(
    "failure_phase",
    [
        pytest.param(("inspect", False), id="inspect-explicit-remove"),
        pytest.param(("stop", False), id="stop-explicit-remove"),
        pytest.param(("rm", False), id="remove"),
        pytest.param(("inspect", True), id="inspect-auto-remove"),
        pytest.param(("stop", True), id="stop-auto-remove"),
    ],
)
@pytest.mark.parametrize(
    "cleanup_outcome", ["missing", "error", "wrong-missing", "interrupt", "exit"]
)
@pytest.mark.parametrize("body_outcome", ["success", "error", "interrupt", "exit"])
def test_container_cleanup_disappearance_races(
    failure_phase: tuple[str, bool], cleanup_outcome: str, body_outcome: str
) -> None:
    phase, auto_remove = failure_phase
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    container = pywhales.Container(
        client.client_config, "resource", is_immutable_id=True
    )
    body_error = {
        "success": None,
        "error": ValueError("body"),
        "interrupt": KeyboardInterrupt("body"),
        "exit": SystemExit("body"),
    }[body_outcome]
    cleanup_error = {
        "interrupt": KeyboardInterrupt("cleanup"),
        "exit": SystemExit("cleanup"),
    }.get(cleanup_outcome)
    stderr = {
        "missing": b"No such container: resource",
        "error": b"permission denied",
        "wrong-missing": b"No such image: resource",
        "interrupt": b"",
        "exit": b"",
    }[cleanup_outcome]
    missing = cleanup_outcome == "missing"
    snapshot = json.dumps(
        [
            {
                "Id": "resource",
                "HostConfig": {"AutoRemove": auto_remove},
                "State": {"Running": True},
            }
        ]
    ).encode()

    def response(command: list[str], **kwargs: object) -> CompletedProcess[bytes]:
        if command[2] == phase:
            if cleanup_error is not None:
                raise cleanup_error
            return CompletedProcess(command, 1, b"", stderr)
        return CompletedProcess(command, 0, snapshot, b"")

    def use_resource() -> None:
        with container:
            if body_error is not None:
                raise body_error

    with (
        patch("subprocess.run", side_effect=response) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        if missing and body_error is None:
            use_resource()
        else:
            with pytest.raises(BaseException) as raised:
                use_resource()
            if missing:
                assert raised.value is body_error
            else:
                if body_error is None:
                    cleanup_failure = raised.value
                else:
                    assert isinstance(raised.value, BaseExceptionGroup)
                    assert (
                        raised.value.message == "Container body and cleanup both failed"
                    )
                    assert len(raised.value.exceptions) == 2
                    assert raised.value.exceptions[0] is body_error
                    assert raised.value.__cause__ is None
                    assert raised.value.__suppress_context__ is True
                    cleanup_failure = raised.value.exceptions[1]
                if cleanup_error is not None:
                    assert cleanup_failure is cleanup_error
                else:
                    expected_type = (
                        NoSuchImage
                        if cleanup_outcome == "wrong-missing"
                        else DockerException
                    )
                    assert isinstance(cleanup_failure, DockerException)
                    assert type(cleanup_failure) is expected_type
                    assert cleanup_failure.stderr == stderr.decode()
                    assert cleanup_failure.return_code == 1
                    assert cleanup_failure.docker_command == command.call_args.args[0]
        phases = [call.args[0][2] for call in command.call_args_list]
        assert (
            phases
            == ["inspect", "stop", "rm"][: ["inspect", "stop", "rm"].index(phase) + 1]
        )


def build_image(ctr_client: DockerClient, python_code: str) -> Image:
    """Build and load a fixed-tag Python image from temporary source files.

    Requires Docker and Buildx. The build context is deleted before return, but
    the loaded image and its some_image tag remain on the selected daemon.

    Args:
        ctr_client: Client targeting the disposable test daemon.
        python_code: Script copied into the image and run by its default command.

    Returns:
        The loaded image handle; this helper does not remove it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "file.py").write_text(python_code)
        (tmpdir / "Dockerfile").write_text(
            f"""
FROM python:{sys.version_info[0]}.{sys.version_info[1]}
COPY file.py /file.py
CMD python /file.py
        """
        )
        image = ctr_client.build(tmpdir, tags="some_image", load=True)
        assert image is not None
        return image


@pytest.mark.parametrize("json_file", get_all_jsons("containers"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    ContainerInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


def test_simple_command(ctr_client: DockerClient) -> None:
    output = ctr_client.run("hello-world", remove=True)
    assert "Hello from Docker!" in output


def test_simple_mistake_on_run(ctr_client: DockerClient) -> None:
    with pytest.raises(TypeError) as err:
        ctr_client.run("ubuntu", "ls")
    assert "docker.run('ubuntu', ['ls'], ...)" in str(err)
    ctr_client.run("ubuntu", ["ls"], remove=True)


def test_simple_command_create_start(ctr_client: DockerClient) -> None:
    output = ctr_client.container.create("hello-world", remove=True).start(attach=True)
    assert "Hello from Docker!" in output


def test_simple_stream(ctr_client: DockerClient) -> None:
    output = ctr_client.run("hello-world", remove=True, stream=True)

    assert ("stdout", b"Hello from Docker!\n") in list(output)


def test_simple_stream_create_start(ctr_client: DockerClient) -> None:
    container = ctr_client.container.create("hello-world", remove=True)
    output = container.start(attach=True, stream=True)
    assert ("stdout", b"Hello from Docker!\n") in list(output)


def test_same_output_run_create_start(ctr_client: DockerClient) -> None:
    python_code = """
import sys
sys.stdout.write("everything is fine\\n\\nhello world")
sys.stderr.write("Something is wrong!")
    """
    image = build_image(ctr_client, python_code)
    output_run = ctr_client.run(image, remove=True)
    output_create = ctr_client.container.create(image, remove=True).start(attach=True)
    assert output_run == output_create


def test_same_stream_run_create_start(ctr_client: DockerClient) -> None:
    python_code = """
import sys
sys.stdout.write("everything is fine\\n\\nhello world")
sys.stderr.write("Something is wrong!")
    """
    image = build_image(ctr_client, python_code)
    output_run = set(ctr_client.run(image, remove=True, stream=True))
    container = ctr_client.container.create(image, remove=True)
    output_create = set(container.start(attach=True, stream=True))
    assert output_run == output_create


def test_exact_output(ctr_client: DockerClient) -> None:
    with contextlib.suppress(DockerException):
        ctr_client.image.remove("busybox")
    assert ctr_client.run("busybox", ["echo", "dodo"], remove=True) == "dodo"


def test_fails_correctly(ctr_client: DockerClient) -> None:
    python_code = """
import sys
sys.stdout.write("everything is fine")
sys.stderr.write("Something is wrong!")
sys.exit(1)
"""
    image = build_image(ctr_client, python_code)
    with image:
        with pytest.raises(DockerException) as err:
            for _ in ctr_client.run(image, stream=True, remove=True):
                pass
        assert err.value.stderr is not None
        assert "Something is wrong!" in err.value.stderr


def test_container_repr(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "ubuntu",
        ["sleep", "infinity"],
        remove=True,
        detach=True,
        stop_timeout=1,
    ) as container:
        assert container.id[:12] in repr(ctr_client.container.list())


def test_run_with_random_port(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "ubuntu",
        ["sleep", "infinity"],
        publish=[(90,)],
        remove=True,
        detach=True,
        stop_timeout=1,
    ) as container:
        assert container.network_settings is not None
        ports = container.network_settings.ports
        assert ports is not None
        assert ports["90/tcp"] is not None
        bindings = ports["90/tcp"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        assert binding["HostPort"] is not None
        assert container.id[:12] in repr(ctr_client.container.list())


def test_create_with_random_ports(ctr_client: DockerClient) -> None:
    with ctr_client.container.create(
        "ubuntu", ["sleep", "infinity"], publish=[(90,)], stop_timeout=1
    ) as container:
        container.start()
        assert container.network_settings is not None
        ports = container.network_settings.ports
        assert ports is not None
        assert ports["90/tcp"] is not None
        bindings = ports["90/tcp"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        assert binding["HostPort"] is not None


def test_create_with_cgroupns(ctr_client: DockerClient) -> None:
    with ctr_client.container.run(
        "ubuntu", ["sleep", "infinity"], cgroupns="host", detach=True, stop_timeout=1
    ) as container:
        assert container.host_config is not None
        assert container.host_config.cgroupns_mode == "host"


def test_fails_correctly_create_start(ctr_client: DockerClient) -> None:
    python_code = """
import sys
sys.stdout.write("everything is fine")
sys.stderr.write("Something is wrong!")
sys.exit(1)
"""
    image = build_image(ctr_client, python_code)
    with image:
        container = ctr_client.container.create(image, remove=True)
        with pytest.raises(DockerException) as err:
            for _ in container.start(attach=True, stream=True):
                pass
        assert err.value.stderr is not None
        assert "Something is wrong!" in err.value.stderr


def test_remove_on_exit(ctr_client: DockerClient) -> None:
    output = ctr_client.run("hello-world", remove=True)
    assert "Hello from Docker!" in output


def test_cpus(ctr_client: DockerClient) -> None:
    output = ctr_client.run("hello-world", cpus=1.5, remove=True)
    assert "Hello from Docker!" in output


def test_run_volumes(ctr_client: DockerClient) -> None:
    volume_name = random_name()
    ctr_client.run(
        "busybox",
        ["touch", "/some/path/dodo"],
        volumes=[(volume_name, "/some/path")],
        remove=True,
    )
    ctr_client.volume.remove(volume_name)


def test_remove_manually(ctr_client: DockerClient) -> None:
    container = ctr_client.run("hello-world", detach=True)
    time.sleep(0.3)
    assert container in ctr_client.container.list(all=True)
    ctr_client.container.remove(container)
    assert container not in ctr_client.container.list(all=True)


@pytest.mark.parametrize("source", ["stdout", "stderr"])
@pytest.mark.parametrize("follow", [False, True])
def test_eager_container_logs_release_child_when_decoding_fails(
    source: str, follow: bool
) -> None:
    client = DockerClient(client_call=[sys.executable])
    with (
        stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                "import sys; print('ready', flush=True); sys.stdin.buffer.read(1); "
                f"sys.{source}.buffer.write(b'\\xff\\n'); sys.{source}.buffer.flush(); "
                "sys.stdin.buffer.read(1)",
            ],
            pipe_stdin=True,
        ) as raw,
        patch.object(client.container, "inspect"),
        patch(
            "pywhales.components.container.cli_wrapper.stream_stdout_and_stderr",
            return_value=raw,
        ) as start,
    ):
        assert raw._queue.get(timeout=3) == ("stdout", b"ready\n")
        assert raw.stdin is not None
        raw.stdin.write(b"x")
        raw.stdin.flush()

        with pytest.raises(UnicodeDecodeError):
            client.container.logs("workload", follow=follow)

        assert raw._process.returncode == (
            1 if sys.platform == "win32" else -signal.SIGKILL
        )
        assert all(not thread.is_alive() for thread in raw._threads)
        assert all(
            pipe.closed
            for pipe in (raw.stdin, raw._process.stdout, raw._process.stderr)
            if pipe is not None
        )
        assert ("--follow" in start.call_args.args[0]) is follow


@pytest.mark.parametrize("source", ["stdout", "stderr"])
def test_eager_container_logs_preserve_text_when_child_exits(source: str) -> None:
    client = DockerClient(client_call=[sys.executable])
    with (
        stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                f"import sys; sys.{source}.buffer.write(b'first\\nlast')",
            ]
        ) as raw,
        patch.object(client.container, "inspect"),
        patch(
            "pywhales.components.container.cli_wrapper.stream_stdout_and_stderr",
            return_value=raw,
        ),
    ):
        output = client.container.logs("workload")

        assert output == "first\nlast"
        assert raw._process.returncode == 0
        assert all(not thread.is_alive() for thread in raw._threads)


@pytest.mark.parametrize("source", ["stdout", "stderr"])
@pytest.mark.parametrize("follow", [False, True])
def test_streaming_container_logs_retain_caller_ownership(
    source: str, follow: bool
) -> None:
    client = DockerClient(client_call=[sys.executable])
    with (
        stream_stdout_and_stderr(
            [
                sys.executable,
                "-c",
                "import sys; print('ready', flush=True); sys.stdin.buffer.read(1); "
                f"sys.{source}.buffer.write(b'\\xff\\n'); sys.{source}.buffer.flush(); "
                "sys.stdin.buffer.read(1)",
            ],
            pipe_stdin=True,
        ) as raw,
        patch.object(client.container, "inspect"),
        patch(
            "pywhales.components.container.cli_wrapper.stream_stdout_and_stderr",
            return_value=raw,
        ) as start,
    ):
        assert raw._queue.get(timeout=3) == ("stdout", b"ready\n")

        output = client.container.logs("workload", follow=follow, stream=True)

        assert output is raw
        assert raw._process.poll() is None
        assert raw.stdin is not None
        raw.stdin.write(b"x")
        raw.stdin.flush()
        assert next(output) == (source, b"\xff\n")
        assert raw._process.poll() is None
        assert ("--follow" in start.call_args.args[0]) is follow
        output.close()
        assert raw._process.returncode is not None
        assert all(not thread.is_alive() for thread in raw._threads)


def test_simple_logs(ctr_client: DockerClient) -> None:
    with ctr_client.run("busybox:1", ["echo", "dodo"], detach=True) as c:
        time.sleep(0.3)
        output = ctr_client.container.logs(c)
        assert output == "dodo\n"


def test_simple_logs_stderr(ctr_client: DockerClient) -> None:
    with ctr_client.run("busybox:1", ["sh", "-c", ">&2 echo dodo"], detach=True) as c:
        time.sleep(0.3)
        output = ctr_client.container.logs(c)
        assert output == "dodo\n"


def test_simple_logs_do_not_follow(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sh", "-c", "sleep 3 && echo dodo"], detach=True
    ) as c:
        output = ctr_client.container.logs(c, follow=False)
        assert output == ""


def test_simple_logs_follow(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sh", "-c", "sleep 3 && echo dodo"], detach=True
    ) as c:
        output = ctr_client.container.logs(c, follow=True)
        assert output == "dodo\n"


def test_simple_logs_stream(ctr_client: DockerClient) -> None:
    with ctr_client.run("busybox:1", ["echo", "dodo"], detach=True) as c:
        time.sleep(0.3)
        output = list(ctr_client.container.logs(c, stream=True))
        assert output == [("stdout", b"dodo\n")]


def test_simple_logs_stream_stderr(ctr_client: DockerClient) -> None:
    with ctr_client.run("busybox:1", ["sh", "-c", ">&2 echo dodo"], detach=True) as c:
        time.sleep(0.3)
        output = list(ctr_client.container.logs(c, stream=True))
        assert output == [("stderr", b"dodo\n")]


def test_simple_logs_stream_stdout_and_stderr(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sh", "-c", ">&2 echo dodo && echo dudu"], detach=True
    ) as c:
        time.sleep(0.3)
        output = list(ctr_client.container.logs(c, stream=True))

        # the order of stderr and stdout is not guaranteed.
        assert set(output) == {("stderr", b"dodo\n"), ("stdout", b"dudu\n")}


def test_simple_logs_stream_wait(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sh", "-c", "sleep 3 && echo dodo"], detach=True
    ) as c:
        time.sleep(0.3)
        output = list(ctr_client.container.logs(c, follow=True, stream=True))
        assert output == [("stdout", b"dodo\n")]


def test_rename(ctr_client: DockerClient) -> None:
    name = random_name()
    new_name = random_name()
    assert name != new_name
    container = ctr_client.container.run("hello-world", name=name, detach=True)
    ctr_client.container.rename(container, new_name)
    container.reload()

    assert container.name == new_name
    ctr_client.container.remove(container)


def test_name(ctr_client: DockerClient) -> None:
    name = random_name()
    container = ctr_client.container.run("hello-world", name=name, detach=True)
    assert container.name == name
    ctr_client.container.remove(container)


json_state = """
{
    "Status": "running",
    "Running": true,
    "Paused": false,
    "Restarting": false,
    "OOMKilled": false,
    "Dead": false,
    "Pid": 1462,
    "ExitCode": 0,
    "Error": "",
    "StartedAt": "2020-09-02T20:14:54.3151689Z",
    "FinishedAt": "2020-09-02T22:14:50.4625972+02:00"
}
"""


def test_container_state(ctr_client: DockerClient) -> None:
    a = ContainerState(**json.loads(json_state))

    assert a.status == "running"
    assert a.running is True
    assert a.exit_code == 0
    assert a.finished_at == datetime(
        2020, 9, 2, 22, 14, 50, 462597, tzinfo=timezone(timedelta(hours=2))
    )


def test_restart(ctr_client: DockerClient) -> None:
    cmd = ["sleep", "infinity"]
    with contextlib.ExitStack() as stack:
        containers = [
            stack.enter_context(ctr_client.run("busybox:1", cmd, detach=True))
            for _ in range(3)
        ]
        ctr_client.kill(containers)
        ctr_client.wait(containers)

        ctr_client.restart(containers)
        for container in containers:
            container.reload()
            assert container.state is not None
            assert container.state.running is True


def test_execute(ctr_client: DockerClient) -> None:
    my_container = ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True, remove=True
    )
    exec_result = ctr_client.execute(my_container, ["echo", "dodo"])
    assert exec_result == "dodo"
    ctr_client.kill(my_container)


def test_execute_simple_mistake(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True, remove=True
    ) as my_container:
        with pytest.raises(TypeError) as err:
            ctr_client.execute(my_container, "echo dodo")
        assert f"docker.execute('{my_container.name}', ['echo', 'dodo'], ...)" in str(
            err
        )


def test_execute_stream(ctr_client: DockerClient) -> None:
    my_container = ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True, remove=True
    )
    exec_result = list(
        ctr_client.execute(my_container, ["sh", "-c", ">&2 echo dodo"], stream=True)
    )
    assert exec_result == [("stderr", b"dodo\n")]
    ctr_client.kill(my_container)


def test_execute_interactive_stream(ctr_client: DockerClient) -> None:
    my_container = ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True, remove=True
    )
    proc = ctr_client.execute(my_container, ["cat"], interactive=True, stream=True)
    assert proc.stdin is not None
    proc.stdin.write(b"hello\n")
    proc.stdin.close()
    output = list(proc)
    assert ("stdout", b"hello\n") in output
    ctr_client.kill(my_container)


def test_diff(ctr_client: DockerClient) -> None:
    my_container = ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True, remove=True
    )

    ctr_client.execute(my_container, ["mkdir", "/some_path"])
    ctr_client.execute(my_container, ["touch", "/some_file"])
    ctr_client.execute(my_container, ["rm", "-rf", "/tmp"])

    diff = ctr_client.diff(my_container)
    assert diff == {"/some_path": "A", "/some_file": "A", "/tmp": "D"}
    ctr_client.kill(my_container)


def test_methods(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sleep", "infinity"], detach=True
    ) as my_container:
        my_container.kill()
        ctr_client.wait(my_container)
        my_container.reload()
        assert my_container.state is not None
        assert my_container.state.running is not True


def test_kill_signal(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox:1", ["sleep", "infinity"], init=True, detach=True
    ) as my_container:
        my_container.kill(signal=signal.SIGINT)
        assert ctr_client.wait(my_container) == 130
        my_container.reload()
        assert my_container.state is not None
        assert my_container.state.running is not True
        assert my_container.state.exit_code == 130


def test_context_manager(ctr_client: DockerClient) -> None:
    container_name = random_name()
    with (
        pytest.raises(ArithmeticError),
        ctr_client.run(
            "busybox:1", ["sleep", "infinity"], detach=True, name=container_name
        ),
    ):
        raise ArithmeticError

    assert container_name not in [x.name for x in ctr_client.container.list(all=True)]


def test_context_manager_with_create(ctr_client: DockerClient) -> None:
    container_name = random_name()
    with (
        pytest.raises(ArithmeticError),
        ctr_client.container.create(
            "busybox:1", ["sleep", "infinity"], name=container_name
        ) as c,
    ):
        assert isinstance(c, pywhales.Container)
        raise ArithmeticError

    assert container_name not in [x.name for x in ctr_client.container.list(all=True)]


def test_list_filters(ctr_client: DockerClient) -> None:
    random_label_value = random_name()

    containers_with_labels = []

    for _ in range(3):
        containers_with_labels.append(
            ctr_client.run(
                "busybox",
                ["sleep", "infinity"],
                remove=True,
                detach=True,
                labels={"dodo": random_label_value},
            )
        )

    containers_with_wrong_labels = []
    for _ in range(3):
        containers_with_wrong_labels.append(
            ctr_client.run(
                "busybox",
                ["sleep", "infinity"],
                remove=True,
                detach=True,
                labels={"dodo": "something"},
            )
        )

    expected_containers_with_labels = ctr_client.container.list(
        filters=[("label", f"dodo={random_label_value}")]
    )

    assert set(expected_containers_with_labels) == set(containers_with_labels)

    for container in containers_with_labels + containers_with_wrong_labels:
        container.kill()


def test_list_filters_client(docker_client: DockerClient) -> None:
    random_label_value = random_name()

    containers_with_labels = []
    for _ in range(3):
        containers_with_labels.append(
            docker_client.run(
                "busybox",
                ["sleep", "infinity"],
                remove=True,
                detach=True,
                labels={"dodo": random_label_value},
            )
        )

    containers_with_wrong_labels = []
    for _ in range(3):
        containers_with_wrong_labels.append(
            docker_client.run(
                "busybox",
                ["sleep", "infinity"],
                remove=True,
                detach=True,
                labels={"dodo": "something"},
            )
        )

    expected_containers_with_labels = docker_client.container.list(
        filters=[("label", f"dodo={random_label_value}")]
    )

    assert set(expected_containers_with_labels) == set(containers_with_labels)

    for container in containers_with_labels + containers_with_wrong_labels:
        container.kill()


def test_wait_single_container(ctr_client: DockerClient) -> None:
    cont_1 = ctr_client.run("busybox", ["sh", "-c", "sleep 2 && exit 8"], detach=True)
    with cont_1:
        exit_code = ctr_client.wait(cont_1)

    assert exit_code == 8


def test_wait_single_container_already_finished(ctr_client: DockerClient) -> None:
    cont_1 = ctr_client.run("busybox", ["sh", "-c", "exit 8"], detach=True)
    first_exit_code = ctr_client.wait(cont_1)
    second_exit_code = ctr_client.wait(cont_1)

    assert first_exit_code == second_exit_code == 8


def test_wait_multiple_container(ctr_client: DockerClient) -> None:
    cont_1 = ctr_client.run("busybox", ["sh", "-c", "sleep 2 && exit 8"], detach=True)
    cont_2 = ctr_client.run("busybox", ["sh", "-c", "sleep 2 && exit 10"], detach=True)

    with cont_1, cont_2:
        exit_codes = ctr_client.wait([cont_1, cont_2])

    assert exit_codes == [8, 10]


def test_wait_multiple_container_random_exit_order(ctr_client: DockerClient) -> None:
    cont_1 = ctr_client.run("busybox", ["sh", "-c", "sleep 4 && exit 8"], detach=True)
    cont_2 = ctr_client.run("busybox", ["sh", "-c", "sleep 2 && exit 10"], detach=True)

    with cont_1, cont_2:
        exit_codes = ctr_client.wait([cont_1, cont_2])

    assert exit_codes == [8, 10]


def test_pause_unpause(ctr_client: DockerClient) -> None:
    container = ctr_client.run(
        "busybox", ["ping", "www.google.com"], detach=True, remove=True
    )

    with container:
        assert container.state is not None
        assert container.state.running is True
        container.pause()
        try:
            container.reload()
            assert container.state.paused is True
        finally:
            container.unpause()
        container.reload()
        assert container.state.paused is not True
        assert container.state.running is True


@pytest.mark.parametrize("json_file", get_all_jsons("stats"))
def test_load_stats_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    stats = ContainerStats(json.loads(json_as_txt))
    assert stats.memory_used > 100


def test_stats_all(ctr_client: DockerClient) -> None:
    with ctr_client.run(
        "busybox", ["sleep", "infinity"], detach=True, stop_timeout=1
    ) as container:
        for stat in ctr_client.stats():
            if stat.container_id == container.id:
                break
        else:
            pytest.fail(f"No statistics for container {container.id}")
        assert stat.container_name == container.name
        assert stat.cpu_percentage <= 5
        assert stat.memory_used <= 100_000_000


def test_stats_container(ctr_client: DockerClient) -> None:
    with (
        ctr_client.run("busybox", ["sleep", "infinity"], detach=True) as container,
        ctr_client.run("busybox", ["sleep", "infinity"], detach=True),
    ):
        stats = ctr_client.stats(containers=[container.id])
        assert len(stats) == 1
        assert stats[0].container_name == container.name


@patch("pywhales.components.container.cli_wrapper.run")
def test_stats_cli_default(run_mock: Mock) -> None:
    docker.container.stats()
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "container",
            "stats",
            "--format",
            "{{json .}}",
            "--no-stream",
            "--no-trunc",
        ]
    )


@patch("pywhales.components.container.cli_wrapper.run")
def test_stats_cli_container(run_mock: Mock) -> None:
    test_containers = ["dummy_container_0", "dummy_container_1"]
    docker.container.stats(containers=test_containers)
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "container",
            "stats",
            "--format",
            "{{json .}}",
            "--no-stream",
            "--no-trunc",
            *test_containers,
        ]
    )


def test_stats_cli_empty_selection(ctr_client: DockerClient) -> None:
    assert ctr_client.container.stats(containers=[]) == []


def test_remove_anonymous_volume_too(ctr_client: DockerClient) -> None:
    container = ctr_client.run("postgres:9.6.20-alpine", detach=True)

    assert container.mounts is not None
    volume_id = container.mounts[0].name
    assert volume_id is not None
    volume = ctr_client.volume.inspect(volume_id)

    with container:
        pass

    assert volume not in ctr_client.volume.list()
    assert container not in ctr_client.ps(all=True)


def test_remove_nothing(ctr_client: DockerClient) -> None:
    ctr_client.container.remove([])


def test_stop_nothing(ctr_client: DockerClient) -> None:
    ctr_client.container.stop([])


def test_kill_nothing(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True):
        set_of_containers = set(ctr_client.ps())
        ctr_client.container.kill([])
        assert set_of_containers == set(ctr_client.ps())


@patch("pywhales.components.container.cli_wrapper.run")
def test_start_detach_keys(run_mock: Mock) -> None:
    docker.start("ctr_name", detach_keys="a,b")
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "container",
            "start",
            "--detach-keys",
            "a,b",
            "ctr_name",
        ],
        timeout=None,
    )


def test_exec_env(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True) as c:
        result = c.execute(["bash", "-c", "echo $DODO"], envs={"DODO": "dada"})

    assert result == "dada"


def test_exec_env_file(ctr_client: DockerClient, tmp_path: Path) -> None:
    env_file = tmp_path / "variables.env"
    env_file.write_text("DODO=dada\n")

    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True) as c:
        result = c.execute(["bash", "-c", "echo $DODO"], env_files=[env_file])
    assert result == "dada"


@patch("pywhales.components.container.cli_wrapper.run")
def test_exec_detach_keys(run_mock: Mock) -> None:
    docker.execute("ctr_name", ["cmd"], detach_keys="a,b")
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "exec",
            "--detach-keys",
            "a,b",
            "ctr_name",
            "cmd",
        ],
        tty=False,
        pass_fds=(),
        timeout=None,
        env={},
        workload_status=True,
    )


def test_export_file(ctr_client: DockerClient, tmp_path: Path) -> None:
    dest = tmp_path / "dodo.tar"
    with ctr_client.run(
        "busybox", ["sleep", "infinity"], detach=True, remove=True
    ) as c:
        c.export(dest)

    assert dest.exists()
    assert dest.stat().st_size > 10_000


def test_exec_privilged_flag(ctr_client: DockerClient, mocker: MockerFixture) -> None:
    fake_completed_process = mocker.MagicMock()
    fake_completed_process.returncode = 0

    patched_run = mocker.patch("subprocess.run")
    patched_run.return_value = fake_completed_process
    ctr_client.execute("my_container", ["some_command"], privileged=True)

    assert patched_run.call_args[0][0][1:] == [
        "exec",
        "--privileged",
        "my_container",
        "some_command",
    ]


def test_exec_change_user(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True) as c:
        c.execute(["useradd", "-ms", "/bin/bash", "newuser"])
        assert c.execute(["whoami"], user="newuser") == "newuser"


def test_exec_change_directory(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True) as c:
        assert c.execute(["pwd"], workdir="/tmp") == "/tmp"
        assert c.execute(["pwd"], workdir="/etc") == "/etc"
        assert c.execute(["pwd"], workdir="/usr/lib") == "/usr/lib"


def test_exec_interactive_no_tty(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", ["sleep", "infinity"], detach=True, remove=True) as c:
        with pytest.raises(DockerException) as no_tty_exc:
            c.execute(["/bin/bash", "-c", "hi"], interactive=True, tty=False)
        assert no_tty_exc.value.stdout == ""
        assert no_tty_exc.value.stderr is not None
        assert "hi: command not found" in no_tty_exc.value.stderr


@pytest.mark.parametrize(
    "method",
    [
        "attach",
        "commit",
        "diff",
        "inspect",
        "kill",
        "logs",
        "pause",
        "remove",
        "restart",
        "start",
        "stop",
        "unpause",
        "wait",
    ],
)
def test_functions_nosuchcontainer(ctr_client: DockerClient, method: str) -> None:
    with pytest.raises(NoSuchContainer):
        getattr(ctr_client.container, method)("DOODODGOIHURHURI")


def test_copy_nosuchcontainer(ctr_client: DockerClient) -> None:
    with pytest.raises(NoSuchContainer):
        ctr_client.container.copy(
            ("dodueizbgueirzhgueoz", "/dududada"), "/tmp/dudufeoz"
        )


def test_execute_nosuchcontainer(ctr_client: DockerClient) -> None:
    with pytest.raises(NoSuchContainer):
        ctr_client.container.execute("dodueizbgueirzhgueoz", ["echo", "dudu"])


def test_export_nosuchcontainer(ctr_client: DockerClient, tmp_path: Path) -> None:
    dest = tmp_path / "dodo.tar"
    with pytest.raises(NoSuchContainer):
        ctr_client.container.export("some_random_container_that_does_not_exists", dest)


def test_rename_nosuchcontainer(ctr_client: DockerClient) -> None:
    with pytest.raises(NoSuchContainer):
        ctr_client.container.rename("dodueizbgueirzhgueoz", "new_name")


def test_update_nosuchcontainer(ctr_client: DockerClient) -> None:
    with pytest.raises(NoSuchContainer):
        ctr_client.container.update("grueighuri", cpus=1)


def test_prune(ctr_client: DockerClient) -> None:
    for container in ctr_client.container.list(filters=[("name", "test-container")]):
        ctr_client.container.remove(container, force=True)
    container = ctr_client.container.create("busybox")
    assert container in ctr_client.container.list(all=True)

    # container not pruned because it is not old enough
    ctr_client.container.prune(filters=[("until", "100h")])
    assert container in ctr_client.container.list(all=True)

    # container not pruned because it is does not have label "dne"
    ctr_client.container.prune(filters=[("label", "dne")])
    assert container in ctr_client.container.list(all=True)

    # container not pruned because it is not old enough and does not have label "dne"
    ctr_client.container.prune(filters=[("until", "100h"), ("label", "dne")])
    assert container in ctr_client.container.list(all=True)

    # container pruned
    ctr_client.container.prune()
    assert container not in ctr_client.container.list(all=True)


def test_prune_streaming(ctr_client: DockerClient) -> None:
    for container in ctr_client.container.list(filters=[("name", "test-container")]):
        ctr_client.container.remove(container, force=True)
    container = ctr_client.container.create("busybox")
    assert container in ctr_client.container.list(all=True)

    # container not pruned because it is not old enough
    logs = list(
        ctr_client.container.prune(filters=[("until", "100h")], stream_logs=True)
    )
    assert container in ctr_client.container.list(all=True)

    assert len(logs) >= 1
    logs_as_big_binary = b""
    for log_type, log_value in logs:
        assert log_type in ("stdout", "stderr")
        logs_as_big_binary += log_value

    assert b"Total reclaimed space:" in logs_as_big_binary

    # container not pruned because it is does not have label "dne"
    logs = list(
        ctr_client.container.prune(filters=[("label", "dne")], stream_logs=True)
    )
    assert container in ctr_client.container.list(all=True)

    assert len(logs) >= 1
    logs_as_big_binary = b""
    for log_type, log_value in logs:
        assert log_type in ("stdout", "stderr")
        logs_as_big_binary += log_value

    assert b"Total reclaimed space:" in logs_as_big_binary

    # container not pruned because it is not old enough and does not have label "dne"
    logs = list(
        ctr_client.container.prune(
            filters=[("until", "100h"), ("label", "dne")], stream_logs=True
        )
    )
    assert container in ctr_client.container.list(all=True)

    assert len(logs) >= 1
    logs_as_big_binary = b""
    for log_type, log_value in logs:
        assert log_type in ("stdout", "stderr")
        logs_as_big_binary += log_value

    assert b"Total reclaimed space:" in logs_as_big_binary

    # container pruned
    logs = list(ctr_client.container.prune(stream_logs=True))
    assert container not in ctr_client.container.list(all=True)

    assert len(logs) >= 3

    logs_as_big_binary = b""
    for log_type, log_value in logs:
        assert log_type in ("stdout", "stderr")
        logs_as_big_binary += log_value

    assert b"Total reclaimed space:" in logs_as_big_binary


def test_run_detached_interactive(ctr_client: DockerClient) -> None:
    with ctr_client.run("ubuntu", interactive=True, detach=True, tty=False) as c:
        c.execute(["true"])


@patch("pywhales.components.container.cli_wrapper.ContainerCLI.inspect")
@patch("pywhales.components.container.cli_wrapper.run")
def test_attach_default(run_mock: Mock, inspect_mock: Mock) -> None:
    test_container_name = "test_dummy_container"

    docker.attach(test_container_name)

    inspect_mock.assert_called_once_with(test_container_name)
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "attach",
            "--sig-proxy",
            test_container_name,
        ],
        tty=True,
    )


@patch("pywhales.components.container.cli_wrapper.ContainerCLI.inspect")
@patch("pywhales.components.container.cli_wrapper.run")
def test_attach_detach_keys_argument(run_mock: Mock, inspect_mock: Mock) -> None:
    test_container_name = "test_dummy_container"
    test_detach_key = "dummy"

    docker.attach(test_container_name, detach_keys=test_detach_key)

    inspect_mock.assert_called_once_with(test_container_name)
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "attach",
            "--detach-keys",
            test_detach_key,
            "--sig-proxy",
            test_container_name,
        ],
        tty=True,
    )


@patch("pywhales.components.container.cli_wrapper.ContainerCLI.inspect")
@patch("pywhales.components.container.cli_wrapper.run")
def test_attach_no_stdin_argument(run_mock: Mock, inspect_mock: Mock) -> None:
    test_container_name = "test_dummy_container"

    docker.attach(test_container_name, stdin=False)

    inspect_mock.assert_called_once_with(test_container_name)
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "attach",
            "--no-stdin",
            "--sig-proxy",
            test_container_name,
        ],
        tty=True,
    )


@patch("pywhales.components.container.cli_wrapper.ContainerCLI.inspect")
@patch("pywhales.components.container.cli_wrapper.run")
def test_attach_sig_proxy_argument(run_mock: Mock, inspect_mock: Mock) -> None:
    test_container_name = "test_dummy_container"

    docker.attach(test_container_name, sig_proxy=False)

    inspect_mock.assert_called_once_with(test_container_name)
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "attach", test_container_name], tty=True
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_create_default_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.create(test_image_name)

    image_cli_mock._pull_if_necessary.assert_called_once_with(test_image_name)
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "create", test_image_name], env={}
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_create_missing_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.create(test_image_name, pull="missing")

    image_cli_mock._pull_if_necessary.assert_called_once_with(test_image_name)
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "create", test_image_name], env={}
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_create_always_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.create(test_image_name, pull="always")

    image_cli_mock._pull_if_necessary.assert_not_called()
    image_cli_mock.pull.assert_called_once_with(test_image_name)
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "create", test_image_name], env={}
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_create_never_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.create(test_image_name, pull="never")

    image_cli_mock._pull_if_necessary.assert_not_called()
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "create",
            "--pull",
            "never",
            test_image_name,
        ],
        env={},
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_run_default_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.run(test_image_name)

    image_cli_mock._pull_if_necessary.assert_called_once_with(test_image_name)
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "container", "run", test_image_name],
        tty=False,
        capture_stderr=False,
        pass_fds=(),
        timeout=None,
        env={},
        workload_status=True,
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_run_missing_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.run(test_image_name, pull="missing")

    image_cli_mock._pull_if_necessary.assert_called_once_with(test_image_name)
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "container", "run", test_image_name],
        tty=False,
        capture_stderr=False,
        pass_fds=(),
        timeout=None,
        env={},
        workload_status=True,
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_run_always_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.run(test_image_name, pull="always")

    image_cli_mock._pull_if_necessary.assert_not_called()
    image_cli_mock.pull.assert_called_once_with(test_image_name)
    run_mock.assert_called_once_with(
        [*docker.client_config.docker_cmd, "container", "run", test_image_name],
        tty=False,
        capture_stderr=False,
        pass_fds=(),
        timeout=None,
        env={},
        workload_status=True,
    )


@patch("pywhales.components.container.cli_wrapper.run")
@patch("pywhales.components.container.cli_wrapper.Container")
@patch("pywhales.components.image.cli_wrapper.ImageCLI")
def test_run_never_pull(image_mock: Mock, _: Mock, run_mock: Mock) -> None:
    image_cli_mock = Mock()
    image_mock.return_value = image_cli_mock

    test_image_name = "test_dummy_image"

    docker.run(test_image_name, pull="never")

    image_cli_mock._pull_if_necessary.assert_not_called()
    image_cli_mock.pull.assert_not_called()
    run_mock.assert_called_once_with(
        [
            *docker.client_config.docker_cmd,
            "container",
            "run",
            "--pull",
            "never",
            test_image_name,
        ],
        tty=False,
        capture_stderr=False,
        pass_fds=(),
        timeout=None,
        env={},
        workload_status=True,
    )


def test_create_never_pull_error(ctr_client: DockerClient) -> None:
    test_image = "alpine:latest"

    if ctr_client.image.exists(test_image):
        ctr_client.image.remove(test_image, force=True)

    with pytest.raises(DockerException):
        ctr_client.container.create(test_image, pull="never")


def test_run_never_pull_error(ctr_client: DockerClient) -> None:
    test_image = "alpine:latest"

    if ctr_client.image.exists(test_image):
        ctr_client.image.remove(test_image, force=True)

    with pytest.raises(DockerException):
        ctr_client.container.run(test_image, pull="never")


def test_create_missing_pull_nonexistent(ctr_client: DockerClient) -> None:
    base_image_name = "alpine:latest"

    if ctr_client.image.exists(base_image_name):
        ctr_client.image.remove(base_image_name, force=True)
    assert not ctr_client.image.exists(base_image_name)
    ctr_client.container.create(base_image_name, pull="missing")
    assert ctr_client.image.exists(base_image_name)


def test_create_missing_pull_existent(
    docker_client: DockerClient, tmp_path: Path, docker_registry: str
) -> None:
    base_image_name = "alpine:latest"
    test_image_name = f"{docker_registry}/{base_image_name}"

    base_image = docker_client.image.pull(base_image_name)
    base_image.tag(test_image_name)
    docker_client.push(test_image_name)
    remote_id = base_image.id

    (tmp_path / "dodo.txt").write_text("Hello world!")
    updated_image = base_image.copy_to(
        tmp_path / "dodo.txt", "/dada.txt", new_tag=test_image_name
    )
    local_id = updated_image.id

    assert remote_id != local_id
    docker_client.container.create(test_image_name, pull="missing")
    assert docker_client.image.inspect(test_image_name).id == local_id


def test_run_missing_pull_nonexistent(ctr_client: DockerClient) -> None:
    base_image_name = "alpine:latest"

    if ctr_client.image.exists(base_image_name):
        ctr_client.image.remove(base_image_name, force=True)
    assert not ctr_client.image.exists(base_image_name)
    ctr_client.container.run(base_image_name, pull="missing")
    assert ctr_client.image.exists(base_image_name)


def test_run_missing_pull_existent(
    docker_client: DockerClient, tmp_path: Path, docker_registry: str
) -> None:
    base_image_name = "alpine:latest"
    test_image_name = f"{docker_registry}/{base_image_name}"

    base_image = docker_client.image.pull(base_image_name)
    base_image.tag(test_image_name)
    docker_client.push(test_image_name)
    remote_id = base_image.id

    (tmp_path / "dodo.txt").write_text("Hello world!")
    updated_image = base_image.copy_to(
        tmp_path / "dodo.txt", "/dada.txt", new_tag=test_image_name
    )
    local_id = updated_image.id

    assert remote_id != local_id
    docker_client.container.run(test_image_name, pull="missing")
    assert docker_client.image.inspect(test_image_name).id == local_id


def test_create_always_pull_nonexistent(ctr_client: DockerClient) -> None:
    base_image_name = "alpine:latest"

    if ctr_client.image.exists(base_image_name):
        ctr_client.image.remove(base_image_name, force=True)
    assert not ctr_client.image.exists(base_image_name)
    ctr_client.container.create(base_image_name, pull="always")
    assert ctr_client.image.exists(base_image_name)


def test_create_always_pull_existent(
    docker_client: DockerClient, tmp_path: Path, docker_registry: str
) -> None:
    base_image_name = "alpine:latest"
    test_image_name = f"{docker_registry}/{base_image_name}"

    base_image = docker_client.image.pull(base_image_name)
    base_image.tag(test_image_name)
    docker_client.push(test_image_name)
    remote_id = docker_client.image.pull(test_image_name).id

    (tmp_path / "dodo.txt").write_text("Hello world!")
    updated_image = base_image.copy_to(
        tmp_path / "dodo.txt", "/dada.txt", new_tag=test_image_name
    )
    local_id = updated_image.id

    assert remote_id != local_id
    with docker_client.container.create(test_image_name, pull="always"):
        assert docker_client.image.inspect(test_image_name).id == remote_id


def test_run_always_pull_nonexistent(ctr_client: DockerClient) -> None:
    base_image_name = "alpine:latest"

    if ctr_client.image.exists(base_image_name):
        ctr_client.image.remove(base_image_name, force=True)
    assert not ctr_client.image.exists(base_image_name)
    ctr_client.container.run(base_image_name, pull="always")
    assert ctr_client.image.exists(base_image_name)


def test_run_always_pull_existent(
    docker_client: DockerClient, tmp_path: Path, docker_registry: str
) -> None:
    base_image_name = "alpine:latest"
    test_image_name = f"{docker_registry}/{base_image_name}"

    base_image = docker_client.image.pull(base_image_name)
    base_image.tag(test_image_name)
    docker_client.push(test_image_name)
    remote_id = docker_client.image.pull(test_image_name).id

    (tmp_path / "dodo.txt").write_text("Hello world!")
    updated_image = base_image.copy_to(
        tmp_path / "dodo.txt", "/dada.txt", new_tag=test_image_name
    )
    local_id = updated_image.id

    assert remote_id != local_id
    with docker_client.container.run(test_image_name, pull="always", detach=True):
        assert docker_client.image.inspect(test_image_name).id == remote_id


def test_non_unicode_output(ctr_client: DockerClient) -> None:
    """Decode invalid UTF-8 container output with a replacement character."""
    latin_char = "þ".encode("latin")
    with pytest.raises(UnicodeDecodeError):
        latin_char.decode(encoding="utf-8")
    byte_repr = rf"\x{ord(latin_char):x}"
    output = ctr_client.container.run(
        "ubuntu", ["bash", "-c", f"echo -n $'{byte_repr}'"], remove=True
    )
    assert output == "�"


def _container_ids(count: int) -> list[str]:
    return [identity_of("container", index) for index in range(count)]


def test_hydrated_container_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _container_ids(5)
    cli = FakeCli("container", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        containers = client.container.list(hydrate=True)
        assert [container.id for container in containers] == ids
        launches = len(cli.launches)
        assert [container.name for container in containers] == [
            f"c-{identity[:6]}" for identity in ids
        ]
        assert all(
            container.state is not None and container.state.running is False
            for container in containers
        )
    assert launches == 4
    assert cli.lists() == [["docker-test", "container", "list", "-q", "--no-trunc"]]
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]
    assert len(cli.launches) == launches


def test_hydrated_container_list_keeps_filters_and_alias() -> None:
    ids = _container_ids(2)
    cli = FakeCli("container", ids)
    with fake_daemon(cli) as client:
        containers = client.ps(True, [("status", "exited")], hydrate=True)
        assert [container.id for container in containers] == ids
        hydrate = inspect.signature(client.container.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.lists()[0] == [
        "docker-test",
        "container",
        "list",
        "-q",
        "--no-trunc",
        "--all",
        "--filter",
        "status=exited",
    ]


def test_default_container_list_stays_lazy_and_expiring() -> None:
    ids = _container_ids(3)
    cli = FakeCli("container", ids)
    with fake_daemon(cli) as client:
        containers = client.container.list()
        assert [container.id for container in containers] == ids
        assert cli.inspects() == []
        assert containers[0].state is not None
        assert containers[0].state is not None
    assert cli.inspects() == [[ids[0]], [ids[0]]]


def test_hydrated_container_list_without_containers_launches_no_inspect() -> None:
    cli = FakeCli("container", [])
    with fake_daemon(cli) as client:
        assert client.container.list(hydrate=True) == []
    assert cli.inspects() == []
    assert len(cli.launches) == 1


def test_vanished_container_reprobes_only_the_affected_chunk() -> None:
    ids = _container_ids(6)
    cli = FakeCli("container", ids)
    cli.vanish_on_list = {ids[4]}
    with fake_daemon(cli, chunk_size=3) as client:
        containers = client.container.list(hydrate=True)
        launches = len(cli.launches)
        assert [container.id for container in containers] == [
            identity for identity in ids if identity != ids[4]
        ]
        assert all(container.state is not None for container in containers)
    assert len(cli.launches) == launches
    assert cli.inspects() == [
        ids[:3],
        ids[3:],
        [ids[3]],
        ids[4:],
        [ids[4]],
        [ids[5]],
    ]


def test_hydrated_container_list_is_empty_when_every_container_vanished() -> None:
    ids = _container_ids(3)
    cli = FakeCli("container", ids)
    cli.vanish_on_list = set(ids)
    with fake_daemon(cli) as client:
        assert client.container.list(hydrate=True) == []
    assert [len(batch) for batch in cli.inspects()] == [3, 1, 2, 1, 1]


def test_hydrated_container_list_does_not_hide_other_inspect_failures() -> None:
    ids = _container_ids(4)
    cli = FakeCli("container", ids)

    def fail_inspect(argv: list[str], *args: object, **kwargs: object) -> object:
        if argv[1:3] == ["container", "inspect"]:
            return CompletedProcess(argv, 1, b"", b"Error response from daemon: boom")
        return cli(argv, *args, **kwargs)

    with (
        fake_daemon(cli) as client,
        patch("subprocess.run", side_effect=fail_inspect),
        pytest.raises(DockerException) as caught,
    ):
        client.container.list(hydrate=True)
    assert type(caught.value) is DockerException
    assert len(cli.launches) == 1


def test_hydrated_container_list_matches_results_by_exact_identity() -> None:
    ids = _container_ids(3)
    cli = FakeCli("container", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        containers = client.container.list(hydrate=True)
        for container in containers:
            assert container._inspect_result is not None
            assert container._inspect_result.id == container.id
        cli.corrupt = {ids[1]: identity_of("container", 99)}
        with pytest.raises(ParsingError):
            client.container.list(hydrate=True)


def test_reload_ends_the_pin_of_a_hydrated_container() -> None:
    ids = _container_ids(2)
    cli = FakeCli("container", ids)
    with fake_daemon(cli) as client:
        first, second = client.container.list(hydrate=True)
        before = len(cli.launches)
        assert first.state is not None
        assert len(cli.launches) == before
        first.reload()
        assert len(cli.launches) == before + 1
        assert first.state is not None
        assert len(cli.launches) == before + 2
        assert second.state is not None
        assert len(cli.launches) == before + 2


_MUTATIONS: dict[str, Callable[[DockerClient, pywhales.Container], object]] = {
    "handle-stop": lambda client, container: container.stop(),
    "handle-kill": lambda client, container: container.kill(),
    "handle-pause": lambda client, container: container.pause(),
    "handle-unpause": lambda client, container: container.unpause(),
    "handle-restart": lambda client, container: container.restart(),
    "handle-rename": lambda client, container: container.rename("other"),
    "handle-start": lambda client, container: container.start(),
    "handle-start-attached": lambda client, container: container.start(attach=True),
    "handle-execute": lambda client, container: container.execute(["true"]),
    "handle-execute-detached": lambda client, container: container.execute(
        ["true"], detach=True
    ),
    "handle-execute-stream": lambda client, container: container.execute(
        ["true"], stream=True
    ).close(),
    "handle-copy-to": lambda client, container: container.copy_to("source", "/target"),
    "cli-copy-to": lambda client, container: client.container.copy(
        "source", (container, "/target")
    ),
    "root-execute": lambda client, container: client.execute(container, ["true"]),
    "cli-stop": lambda client, container: client.container.stop([container]),
    "cli-start": lambda client, container: client.container.start(container),
    "cli-update": lambda client, container: client.container.update(
        container, memory=1
    ),
    "root-restart": lambda client, container: client.restart(container),
}


@pytest.mark.parametrize("mutation", list(_MUTATIONS))
def test_mutating_call_releases_only_that_handles_pin(mutation: str) -> None:
    ids = _container_ids(2)
    cli = FakeCli("container", ids)
    with (
        fake_daemon(cli) as client,
        patch("pywhales.components.container.cli_wrapper.stream_stdout_and_stderr"),
    ):
        mutated, untouched = client.container.list(hydrate=True)
        _MUTATIONS[mutation](client, mutated)
        before = len(cli.inspects())
        assert untouched.state is not None
        assert len(cli.inspects()) == before
        assert mutated.state is not None
        assert len(cli.inspects()) == before + 1


@pytest.mark.parametrize(
    "mutation",
    ["handle-stop", "handle-execute", "handle-execute-detached", "handle-copy-to"],
)
def test_failed_mutation_still_releases_the_pin(mutation: str) -> None:
    ids = _container_ids(1)
    cli = FakeCli("container", ids)

    def fail_mutation(argv: list[str], *args: object, **kwargs: object) -> object:
        if argv[1] == "exec" or argv[1:3] in [
            ["container", "stop"],
            ["container", "cp"],
        ]:
            return CompletedProcess(argv, 1, b"", b"Error: No such container: gone")
        return cli(argv, *args, **kwargs)

    with fake_daemon(cli) as client:
        (container,) = client.container.list(hydrate=True)
        with (
            patch("subprocess.run", side_effect=fail_mutation),
            pytest.raises(NoSuchContainer),
        ):
            _MUTATIONS[mutation](client, container)
        before = len(cli.inspects())
        assert container.state is not None
    assert len(cli.inspects()) == before + 1


def test_removed_hydrated_container_no_longer_reads_from_its_listing() -> None:
    ids = _container_ids(1)
    cli = FakeCli("container", ids)
    with fake_daemon(cli) as client:
        (container,) = client.container.list(hydrate=True)
        container.remove()
        with pytest.raises(NoSuchContainer):
            _ = container.state


@pytest.mark.parametrize(
    ("listed_running", "daemon_running"), [(False, True), (True, False)]
)
def test_context_exit_refreshes_a_pinned_snapshot_before_choosing_stop(
    listed_running: bool, daemon_running: bool
) -> None:
    (identity,) = _container_ids(1)
    cli = FakeCli("container", [identity])
    cli.running(identity, listed_running)
    with fake_daemon(cli, validity=3600) as client:
        (container,) = client.container.list(hydrate=True)
        assert container.state is not None
        assert container.state.running is listed_running
        cli.running(identity, daemon_running)
        launched = len(cli.launches)
        with container:
            pass
    verbs = [launch[2] for launch in cli.launches[launched:]]
    assert verbs == ["inspect", *(["stop"] if daemon_running else []), "rm"]
    assert identity not in cli.present
