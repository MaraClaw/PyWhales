import json
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Service
from pywhales.client_config import ParsingError
from pywhales.components.service.models import ServiceInspectResult
from pywhales.exceptions import (
    DockerException,
    NoSuchConfig,
    NoSuchService,
    NotASwarmManager,
)
from pywhales.test_utils import get_all_jsons, random_name
from pywhales.utils import stream_stdout_and_stderr


def test_batch_inspect_matches_service_references_and_hydrates_cache() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    alpha = {"ID": "a" * 27, "Spec": {"Name": "alpha"}}
    bravo = {"ID": "b" * 27, "Spec": {"Name": "bravo"}}
    references = ["bravo", "a" * 27, "b" * 8, "alpha", "bravo"]
    response = json.dumps([alpha, bravo, alpha, bravo, bravo]).encode()
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 0, response, b"")
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = client.service.inspect(references)
        assert [resource.id for resource in resources] == [
            "b" * 27,
            "a" * 27,
            "b" * 27,
            "a" * 27,
            "b" * 27,
        ]
        assert [
            resource.spec.name for resource in resources if resource.spec is not None
        ] == ["bravo", "alpha", "bravo", "alpha", "bravo"]
        assert all(
            isinstance(resource._get_inspect_result(), ServiceInspectResult)
            for resource in resources
        )
        assert all(
            resource.client_config is client.client_config for resource in resources
        )
        assert resources[0] is not resources[4]
        command.assert_called_once()
        assert command.call_args.args[0] == [
            "docker-test",
            "--host",
            "inventory-host",
            "service",
            "inspect",
            *references,
        ]


def test_batch_inspect_service_scalar_and_lazy_list_contracts() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess(
            [], 0, b'[{"ID":"identity","Spec":{"Name":"named"}}]', b""
        ),
    ) as command:
        resource = client.service.inspect("named")
        assert isinstance(resource, Service)
        assert resource.id == "identity"
        assert isinstance(resource._inspect_result, ServiceInspectResult)
        command.assert_called_once()
        command.return_value = CompletedProcess([], 0, b"identity", b"")
        listed = client.service.list()
        assert listed[0].id == "identity"
        assert listed[0]._inspect_result is None
        assert command.call_count == 3


def test_batch_inspect_empty_service_avoids_executable_lookup() -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        assert client.service.inspect([]) == []


@pytest.mark.parametrize(
    "payload",
    [
        "invalid json",
        "{}",
        "[]",
        "[null,null]",
        "[{},{}]",
        '[{"ID":"first"}]',
        '[{"ID":"first"},{"ID":"second"},{}]',
        '[{"ID":"wrong"},{"ID":"second"}]',
        '[{"ID":""},{"ID":"second"}]',
        '[{"ID":"first"},{"ID":"second","CreatedAt":"invalid"}]',
        '[{"ID":"one","Spec":{"Name":"first"}},{"ID":"two","Spec":{"Name":"first"}}]',
    ],
)
def test_batch_inspect_service_rejects_invalid_response(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        patch.object(Service, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.service.inspect(["first", "second"])
    hydrate.assert_not_called()


def test_batch_inspect_service_preserves_partial_command_failure() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    output = b'[{"ID":"first"}]'
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 17, output, b"permission denied"),
        ) as command,
        patch.object(Service, "_set_inspect_result") as hydrate,
        pytest.raises(DockerException) as failure,
    ):
        client.service.inspect(["first", "second"])
    assert failure.value.return_code == 17
    assert failure.value.stdout == output.decode()
    assert failure.value.stderr == "permission denied"
    assert failure.value.docker_command == [
        "docker-test",
        "service",
        "inspect",
        "first",
        "second",
    ]
    command.assert_called_once()
    hydrate.assert_not_called()


def test_eager_service_logs_release_process_when_decoding_fails() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with stream_stdout_and_stderr(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff\\n'); input()"],
        pipe_stdin=True,
    ) as stream:
        with (
            patch.object(client.service, "inspect"),
            patch(
                "pywhales.components.service.cli_wrapper.stream_stdout_and_stderr",
                return_value=stream,
            ),
            pytest.raises(UnicodeDecodeError),
        ):
            client.service.logs("named")
        assert stream._process.poll() is not None
        assert all(not thread.is_alive() for thread in stream._threads)
        assert all(
            pipe.closed
            for pipe in (stream.stdin, stream._process.stdout, stream._process.stderr)
            if pipe is not None
        )


def test_streaming_service_logs_transfer_process_ownership() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with stream_stdout_and_stderr(
        [sys.executable, "-c", "print('ready', flush=True); input()"], pipe_stdin=True
    ) as stream:
        with (
            patch.object(client.service, "inspect"),
            patch(
                "pywhales.components.service.cli_wrapper.stream_stdout_and_stderr",
                return_value=stream,
            ),
        ):
            result = client.service.logs("named", stream=True, follow=True)
        assert result is stream
        assert next(stream) == ("stdout", b"ready\n")
        assert stream._process.poll() is None


def test_eager_service_logs_preserve_text_result() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        stream_stdout_and_stderr(
            [sys.executable, "-c", "print('hello'); print('world')"]
        ) as stream,
        patch.object(client.service, "inspect"),
        patch(
            "pywhales.components.service.cli_wrapper.stream_stdout_and_stderr",
            return_value=stream,
        ),
    ):
        assert client.service.logs("named") == "hello\nworld\n"
        assert stream._process.poll() == 0


@pytest.mark.parametrize("json_file", get_all_jsons("services"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    ServiceInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


@pytest.mark.usefixtures("swarm_mode")
def test_tasks(docker_client: DockerClient) -> None:
    service = docker_client.service.create("busybox", ["sleep", "infinity"])

    tasks = service.ps()
    assert len(tasks) > 0
    assert tasks[0].desired_state == "running"
    docker_client.service.remove(service)


@pytest.mark.usefixtures("swarm_mode")
def test_get_logs(docker_client: DockerClient) -> None:
    with docker_client.service.create(
        "busybox", ["sh", "-c", "echo dodo && sleep infinity"]
    ) as my_service:
        assert my_service.ps()[0].desired_state == "running"
        assert docker_client.service.logs(my_service).split("|")[-1].strip() == "dodo"


@pytest.mark.usefixtures("swarm_mode")
def test_get_list_of_services(docker_client: DockerClient) -> None:
    with docker_client.service.create(
        "busybox", ["sh", "-c", "echo dodo && sleep infinity"]
    ) as my_service:
        list_of_services = docker_client.service.list()
        assert [my_service] == list_of_services


@pytest.mark.usefixtures("swarm_mode")
def test_list_filters(docker_client: DockerClient) -> None:
    random_label_value = random_name()
    with (
        docker_client.service.create(
            "busybox",
            ["sleep", "infinity"],
            labels={"dodo": random_label_value},
        ) as my_service,
        docker_client.service.create(
            "busybox",
            ["sleep", "infinity"],
            labels={"dodo": "something"},
        ),
    ):
        expected_services = docker_client.service.list(
            filters=[("label", f"dodo={random_label_value}")]
        )
        assert expected_services == [my_service]
        no_expected_services = docker_client.service.list(
            filters=[("label", f"dodo={random_name()}")]
        )
        assert len(no_expected_services) == 0


@pytest.mark.usefixtures("swarm_mode")
def test_get_list_of_services_no_services(docker_client: DockerClient) -> None:
    assert docker_client.service.list() == []


@pytest.mark.usefixtures("swarm_mode")
def test_remove_empty_services_list(docker_client: DockerClient) -> None:
    with docker_client.service.create(
        "busybox", ["sh", "-c", "echo dodo && sleep infinity"]
    ) as my_service:
        assert my_service in docker_client.service.list()
        set_services = set(docker_client.service.list())
        docker_client.service.remove([])
        assert set(docker_client.service.list()) == set_services


@pytest.mark.usefixtures("swarm_mode")
def test_service_scale(docker_client: DockerClient) -> None:
    with docker_client.service.create("busybox", ["sleep", "infinity"]) as service:
        service.scale(3)
        service.reload()
        assert service.spec is not None
        assert service.spec.mode is not None
        assert service.spec.mode["Replicated"] == {"Replicas": 3}
        service.update(replicas=1)
        service.reload()
        assert service.spec is not None
        assert service.spec.mode is not None
        assert service.spec.mode["Replicated"] == {"Replicas": 1}


@pytest.mark.usefixtures("swarm_mode")
def test_context_manager(docker_client: DockerClient) -> None:
    my_service: Service | None = None
    with (
        pytest.raises(RuntimeError),
        docker_client.service.create(
            "busybox", ["sleep", "infinity"]
        ) as created_service,
    ):
        my_service = created_service
        assert created_service.spec is not None
        assert created_service.spec.task_template is not None
        assert created_service.spec.task_template.container_spec is not None
        assert created_service.spec.task_template.container_spec.image is not None
        assert created_service.spec.task_template.container_spec.image.startswith(
            "busybox"
        )
        assert created_service.exists()
        raise RuntimeError

    assert my_service is not None
    assert not my_service.exists()


@pytest.mark.usefixtures("swarm_mode")
def test_service_restart(docker_client: DockerClient) -> None:
    my_service = docker_client.service.create(
        "busybox",
        ["echo", "Hello"],
        detach=True,
        restart_condition="none",
        restart_max_attempts=0,
    )
    time.sleep(2)
    assert my_service.ps()[0].desired_state == "shutdown"
    my_service.remove()


@pytest.mark.usefixtures("swarm_mode")
def test_service_secrets(docker_client: DockerClient) -> None:
    secret_user = None
    secret_pass = None
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"supersecretuser")
        f.seek(0)
        secret_user = docker_client.secret.create("dbuser", f.name)
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"supersecretpass")
        f.seek(0)
        secret_pass = docker_client.secret.create("dbpass", f.name)

    with docker_client.service.create(
        "ubuntu",
        ["bash", "-c", "cat /run/secrets/{dbuser,dbpass} && sleep infinity"],
        secrets=[
            {
                "source": "dbuser",
            },
            {
                "source": "dbpass",
                "uid": "1000",
                "gid": "1000",
                "mode": "0400",
            },
        ],
    ) as my_service:
        assert my_service.ps()[0].desired_state == "running"
    secret_user.remove()
    secret_pass.remove()


@pytest.mark.usefixtures("swarm_mode")
def test_service_mounts(docker_client: DockerClient) -> None:
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"config")
        f.seek(0)
        with docker_client.service.create(
            "ubuntu",
            ["bash", "-c", f"cat {f.name} && sleep infinity"],
            mounts=[
                {
                    "type": "bind",
                    "source": f.name,
                    "destination": f.name,
                },
            ],
        ) as my_service:
            assert my_service.ps()[0].desired_state == "running"


@pytest.mark.parametrize("method", ["inspect", "remove", "ps"])
@pytest.mark.usefixtures("swarm_mode")
def test_some_functions_no_such_service(
    docker_client: DockerClient, method: str
) -> None:
    with pytest.raises(NoSuchService):
        getattr(docker_client.service, method)("DOODODGOIHURHURI")


@pytest.mark.usefixtures("swarm_mode")
def test_scale_no_such_service(docker_client: DockerClient) -> None:
    with pytest.raises(NoSuchService):
        docker_client.service.scale({"DOODODGOIHURHURI": 14})


def test_create_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.create("busybox", ["sleep", "infinity"])

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_inspect_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.inspect("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_exists_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.exists("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_list_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.list()

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_ps_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.ps("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_remove_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.remove("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_scale_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.scale({"dodo": 8})

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_update_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.service.update("dodo", image="busybox")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


EMPTY_SELECTIONS = [
    pytest.param(list, id="list"),
    pytest.param(tuple, id="tuple"),
    pytest.param(lambda: (item for item in ()), id="generator"),
]
SINGLE_SELECTIONS = [
    pytest.param(lambda: "alpha", id="str"),
    pytest.param(lambda: ["alpha"], id="list"),
    pytest.param(lambda: (item for item in ["alpha"]), id="generator"),
]


@pytest.mark.parametrize("make_empty", EMPTY_SELECTIONS)
@pytest.mark.parametrize("method", ["remove", "ps"])
def test_service_empty_selection_skips_cli(
    method: str, make_empty: Callable[[], Iterable[str]]
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        result = getattr(client.service, method)(make_empty())
    assert result == ([] if method == "ps" else None)


def test_service_scale_empty_mapping_skips_inspect_and_cli() -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.service.scale({})


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
@pytest.mark.parametrize(
    ("method", "subcommand"),
    [("remove", ["remove"]), ("ps", ["ps", "--quiet", "--no-trunc"])],
)
def test_service_selection_builds_argv_once(
    method: str,
    subcommand: list[str],
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        getattr(client.service, method)(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "service",
        *subcommand,
        "alpha",
    ]


NOT_A_MANAGER = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    b"and try again."
)
SERVICE_MISSING = b"Error response from daemon: service resource not found"
SERVICE_JSON = b'[{"ID":"resource","Spec":{"Name":"named"}}]'
# Each failure below is not this service's own not-found error.
OTHER_FAILURES = [
    pytest.param(b"permission denied", DockerException, id="command-failure"),
    pytest.param(NOT_A_MANAGER, NotASwarmManager, id="not-a-swarm-manager"),
    pytest.param(
        b"no such task or service: resource", DockerException, id="task-or-service"
    ),
    pytest.param(
        b"Error response from daemon: config resource not found",
        NoSuchConfig,
        id="config-not-found",
    ),
]


def _service() -> tuple[DockerClient, Service]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    return client, Service(client.client_config, "resource", is_immutable_id=True)


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_service_cleanup_ignores_only_a_missing_service(missing: bool) -> None:
    _, service = _service()
    response = CompletedProcess(
        [], 1 if missing else 0, b"", SERVICE_MISSING if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, service:
        pass
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "service", "remove", "resource"]


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_service_cleanup_propagates_other_removal_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, service = _service()
    response = CompletedProcess([], 17, b"output", stderr)
    with (
        patch("subprocess.run", return_value=response) as command,
        pytest.raises(DockerException) as raised,
        service,
    ):
        pass
    command.assert_called_once()
    assert type(raised.value) is error_type
    assert raised.value.return_code == 17
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_service_cleanup_failure_joins_the_body_error(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, service = _service()
    body = RuntimeError("body")
    response = CompletedProcess([], 17, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(BaseExceptionGroup) as raised,
        service,
    ):
        raise body
    assert raised.value.message == "Service body and cleanup both failed"
    assert raised.value.exceptions[0] is body
    assert type(raised.value.exceptions[1]) is error_type


def test_service_missing_at_cleanup_leaves_the_body_error_alone() -> None:
    _, service = _service()
    body = RuntimeError("body")
    response = CompletedProcess([], 1, b"", SERVICE_MISSING)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(RuntimeError) as raised,
        service,
    ):
        raise body
    assert raised.value is body


def _exists(client: DockerClient, service: Service, *, as_handle: bool) -> bool:
    return service.exists() if as_handle else client.service.exists("resource")


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_service_exists_is_true_after_inspect(as_handle: bool) -> None:
    client, service = _service()
    response = CompletedProcess([], 0, SERVICE_JSON, b"")
    with patch("subprocess.run", return_value=response) as command:
        found = _exists(client, service, as_handle=as_handle)
    assert found is True
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "service",
        "inspect",
        "resource",
    ]


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(
    "stderr", [SERVICE_MISSING, b"no such service: resource"], ids=["rm", "inspect"]
)
def test_service_exists_is_false_for_a_missing_service(
    as_handle: bool, stderr: bytes
) -> None:
    client, service = _service()
    response = CompletedProcess([], 1, b"", stderr)
    with patch("subprocess.run", return_value=response):
        found = _exists(client, service, as_handle=as_handle)
    assert found is False


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_service_exists_propagates_other_failures(
    as_handle: bool, stderr: bytes, error_type: type[DockerException]
) -> None:
    client, service = _service()
    response = CompletedProcess([], 1, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(DockerException) as raised,
    ):
        _exists(client, service, as_handle=as_handle)
    assert type(raised.value) is error_type


@pytest.mark.usefixtures("swarm_mode")
def test_service_removed_inside_its_context_exits_cleanly(
    docker_client: DockerClient,
) -> None:
    with docker_client.service.create("busybox", ["sleep", "infinity"]) as my_service:
        assert my_service.exists()
        my_service.remove()
        assert not my_service.exists()
    assert not my_service.exists()
