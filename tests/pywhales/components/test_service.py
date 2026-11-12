import copy
import inspect
import json
import pickle
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
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


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


def _service_ids(count: int) -> list[str]:
    return [identity_of("service", index) for index in range(count)]


def _truncated(ids: list[str]) -> list[str]:
    return [identity[:12] for identity in ids]


def test_hydrated_service_list_resolves_truncated_ids_then_inspects_per_chunk() -> None:
    ids = _service_ids(5)
    cli = FakeCli("service", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        services = client.service.list(hydrate=True)
        launches = len(cli.launches)
        assert [service.id for service in services] == ids
        for service in services:
            assert service._inspect_result is not None
            assert service._inspect_result.id == service.id
        assert [
            service.spec.name for service in services if service.spec is not None
        ] == [f"ser-{index:04d}" for index in range(5)]
    assert launches == 7
    assert cli.lists() == [["docker-test", "service", "list", "--quiet"]]
    truncated = _truncated(ids)
    assert cli.resolves() == [truncated[:2], truncated[2:4], truncated[4:]]
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]
    assert len(cli.launches) == launches


@pytest.mark.parametrize(
    ("count", "chunks"), [(0, []), (100, [100]), (101, [100, 1])], ids=str
)
def test_hydrated_service_list_resolves_and_inspects_at_most_one_chunk_bound(
    count: int, chunks: list[int]
) -> None:
    ids = _service_ids(count)
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        services = client.service.list(hydrate=True)
    assert [service.id for service in services] == ids
    assert [len(batch) for batch in cli.resolves()] == chunks
    assert [len(batch) for batch in cli.inspects()] == chunks


def test_default_service_list_stays_lazy_and_expiring() -> None:
    ids = _service_ids(3)
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        services = client.service.list([("name", "svc")])
        assert [service.id for service in services] == ids
        assert cli.inspects() == []
        assert services[0].spec is not None
        assert services[0].spec is not None
        hydrate = inspect.signature(client.service.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.launches[:2] == [
        ["docker-test", "service", "list", "--quiet", "--filter", "name=svc"],
        [
            "docker-test",
            "service",
            "inspect",
            *_truncated(ids),
            "--format",
            "{{.ID}}",
        ],
    ]
    assert cli.inspects() == [[ids[0]], [ids[0]]]
    assert len(cli.launches) == 4


def test_hydrated_service_list_keeps_filters() -> None:
    ids = _service_ids(2)
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        client.service.list([("label", "tier=web"), ("name", "svc")], hydrate=True)
    assert cli.lists() == [
        [
            "docker-test",
            "service",
            "list",
            "--quiet",
            "--filter",
            "label=tier=web",
            "--filter",
            "name=svc",
        ]
    ]


def test_services_removed_before_resolution_reprobe_only_the_affected_chunk() -> None:
    ids = _service_ids(6)
    truncated = _truncated(ids)
    cli = FakeCli("service", ids)
    cli.vanish_on_list = {ids[4]}
    with fake_daemon(cli, chunk_size=3) as client:
        services = client.service.list(hydrate=True)
        launches = len(cli.launches)
        assert [service.id for service in services] == [
            identity for identity in ids if identity != ids[4]
        ]
        assert all(service.spec is not None for service in services)
    assert len(cli.launches) == launches
    assert cli.resolves() == [
        truncated[:3],
        truncated[3:],
        truncated[3:4],
        truncated[4:],
        truncated[4:5],
        truncated[5:],
    ]
    assert cli.inspects() == [ids[:3], [ids[3], ids[5]]]


def test_services_removed_before_inspection_reprobe_only_the_affected_chunk() -> None:
    ids = _service_ids(6)
    cli = FakeCli("service", ids)
    cli.vanish_on_resolve = {ids[1]}
    with fake_daemon(cli, chunk_size=3) as client:
        services = client.service.list(hydrate=True)
        launches = len(cli.launches)
        assert [service.id for service in services] == [ids[0], *ids[2:]]
        assert all(service.spec is not None for service in services)
    assert len(cli.launches) == launches
    assert len(cli.resolves()) == 2
    assert cli.inspects() == [
        ids[:3],
        ids[:1],
        ids[1:3],
        ids[1:2],
        ids[2:3],
        ids[3:],
    ]


def test_hydrated_service_list_is_empty_when_every_service_vanished() -> None:
    ids = _service_ids(3)
    cli = FakeCli("service", ids)
    cli.vanish_on_list = set(ids)
    with fake_daemon(cli) as client:
        assert client.service.list(hydrate=True) == []
    assert [len(batch) for batch in cli.resolves()] == [3, 1, 2, 1, 1]
    assert cli.inspects() == []


@pytest.mark.parametrize("formatted", [True, False], ids=["resolution", "inspection"])
@pytest.mark.parametrize("failure", list(INSPECT_FAILURES))
def test_hydrated_service_list_does_not_hide_other_failures(
    failure: str, formatted: bool
) -> None:
    ids = _service_ids(4)
    cli = FakeCli("service", ids)
    expected = INSPECT_FAILURES[failure]
    with (
        fake_daemon(cli) as client,
        patch(
            "subprocess.run",
            side_effect=cli.failing({"inspect"}, failure, formatted=formatted),
        ),
        pytest.raises(expected) as caught,
    ):
        client.service.list(hydrate=True)
    assert type(caught.value) is expected
    assert len(cli.resolves()) == 1
    assert len(cli.inspects()) == (0 if formatted else 1)


def test_hydrated_service_list_matches_results_by_exact_identity() -> None:
    ids = _service_ids(3)
    cli = FakeCli("service", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for service in client.service.list(hydrate=True):
            assert service._inspect_result is not None
            assert service._inspect_result.id == service.id
        cli.corrupt = {ids[1]: identity_of("service", 99)}
        with pytest.raises(ParsingError):
            client.service.list(hydrate=True)
    assert len(cli.inspects()) == 1


_SERVICE_MUTATIONS: dict[str, Callable[[DockerClient, Service], object]] = {
    "handle-update": lambda client, service: service.update(replicas=2),
    "handle-scale": lambda client, service: service.scale(2),
    "handle-remove": lambda client, service: service.remove(),
    "cli-update": lambda client, service: client.service.update(service, force=True),
    "cli-scale": lambda client, service: client.service.scale({service: 2}),
    "cli-rollback": lambda client, service: client.service.rollback(service),
    "cli-remove": lambda client, service: client.service.remove([service]),
}


@pytest.mark.parametrize("mutation", list(_SERVICE_MUTATIONS))
def test_mutating_call_releases_only_that_services_pin(mutation: str) -> None:
    ids = _service_ids(2)
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        mutated, untouched = client.service.list(hydrate=True)
        _SERVICE_MUTATIONS[mutation](client, mutated)
        before = len(cli.inspects())
        assert untouched.spec is not None
        assert len(cli.inspects()) == before
        assert mutated.spec is not None
        assert len(cli.inspects()) == before + 1


@pytest.mark.parametrize("mutation", list(_SERVICE_MUTATIONS))
def test_failed_mutation_still_releases_the_services_pin(mutation: str) -> None:
    ids = _service_ids(1)
    cli = FakeCli("service", ids)
    failing = cli.failing({"update", "scale", "rollback", "remove"}, "command")
    with fake_daemon(cli) as client:
        (service,) = client.service.list(hydrate=True)
        with (
            patch("subprocess.run", side_effect=failing),
            pytest.raises(DockerException),
        ):
            _SERVICE_MUTATIONS[mutation](client, service)
        before = len(cli.inspects())
        assert service.spec is not None
    assert len(cli.inspects()) == before + 1


def test_copied_hydrated_service_and_batch_inspected_service_hold_no_pin() -> None:
    ids = _service_ids(1)
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        (service,) = client.service.list(hydrate=True)
        assert service._pinned is True
        assert copy.copy(service)._pinned is False
        assert pickle.loads(pickle.dumps(service))._pinned is False
        (inspected,) = client.service.inspect(ids)
        assert inspected._pinned is False
