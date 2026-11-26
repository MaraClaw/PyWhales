import json
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import assert_type
from unittest.mock import patch

import pytest

from pywhales import DockerClient, docker
from pywhales.client_config import ParsingError
from pywhales.components.task.cli_wrapper import Task, TaskCLI
from pywhales.components.task.models import TaskInspectResult
from pywhales.exceptions import DockerException
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


@pytest.mark.parametrize("json_file", get_all_jsons("tasks"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    TaskInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


@pytest.mark.usefixtures("swarm_mode")
def test_list_tasks() -> None:
    service = docker.service.create("busybox", ["sleep", "infinity"])

    # Todo: use a context manager
    tasks = docker.task.list()
    repr(tasks)
    assert len(tasks) >= 1
    assert tasks[0].desired_state == "running"
    assert tasks[0].service_id == service.id
    docker.service.remove(service)


def _check_inspect_types(api: TaskCLI) -> None:
    assert_type(api.inspect("task"), Task)
    assert_type(api.inspect(["task"]), list[Task])


def test_batch_inspect_preserves_task_aliases_positionally() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    references = ["service.1", "service.2", "service.1"]
    payload = [{"ID": identity} for identity in ["a" * 25, "b" * 25, "a" * 25]]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, json.dumps(payload).encode(), b""),
    ) as command:
        resources = client.task.inspect(references)
    assert isinstance(resources, list)
    assert [resource.id for resource in resources] == ["a" * 25, "b" * 25, "a" * 25]
    assert resources[0] is not resources[2]
    assert all(resource._inspect_result is not None for resource in resources)
    assert all(resource.client_config is client.client_config for resource in resources)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "--host",
        "inventory-host",
        "inspect",
        "--type",
        "task",
        *references,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "null",
        "[1, 2]",
        "[]",
        '[{"ID": "one"}]',
        '[{"ID": "one"}, {"ID": "one"}, {"ID": "one"}]',
        '[{"ID": "one"}, {"ID": 2}]',
        '[{"ID": "one"}, {"ID": ""}]',
        '[{"ID": "one"}, {}]',
        '[{"ID": "one"}, {"ID": "two"}]',
        '[{"ID": "one"}, {"ID": "one", "Slot": 2}]',
    ],
)
def test_batch_inspect_validates_all_tasks_before_hydration(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ) as command,
        patch.object(Task, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.task.inspect(["alias", "alias"])
    command.assert_called_once()
    hydrate.assert_not_called()


def test_batch_inspect_rejects_task_full_id_mismatch_before_hydration() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    payload = json.dumps([{"ID": "a" * 25}, {"ID": "c" * 25}]).encode()
    with (
        patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")),
        patch.object(Task, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.task.inspect(["a" * 25, "b" * 25])
    hydrate.assert_not_called()


def test_batch_inspect_empty_tasks_avoids_lookup() -> None:
    with patch("shutil.which") as lookup, patch("subprocess.run") as command:
        assert DockerClient().task.inspect([]) == []
    lookup.assert_not_called()
    command.assert_not_called()


def test_list_without_services_makes_no_service_ps_call() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        assert client.task.list() == []
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "service", "list", "--quiet"]


def test_list_passes_listed_services_to_one_service_ps() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    responses = [b"short\n", b"full-id\n", b"task-id\n"]
    with patch(
        "subprocess.run",
        side_effect=[CompletedProcess([], 0, out, b"") for out in responses],
    ) as command:
        tasks = client.task.list()
    assert [task.id for task in tasks] == ["task-id"]
    assert [call.args[0] for call in command.call_args_list] == [
        ["docker-test", "service", "list", "--quiet"],
        ["docker-test", "service", "inspect", "short", "--format", "{{.ID}}"],
        ["docker-test", "service", "ps", "--quiet", "--no-trunc", "full-id"],
    ]


def test_hydrated_task_list_batches_across_services() -> None:
    services = [identity_of("service", index) for index in range(2)]
    ids = [identity_of("task", index) for index in range(5)]
    service_cli = FakeCli("service", services)
    task_cli = FakeCli("task", ids)
    task_cli.reorder = True
    owners = [services[0], *([services[1]] * 4)]
    for identity, owner in zip(ids, owners, strict=True):
        task_cli.present[identity]["ServiceID"] = owner

    def run_fake(argv: Sequence[str], **kwargs: object) -> CompletedProcess[bytes]:
        fake = (
            service_cli
            if argv[1:3] in (["service", "list"], ["service", "inspect"])
            else task_cli
        )
        return fake(argv, **kwargs)

    with (
        fake_daemon(task_cli, chunk_size=2) as client,
        patch("subprocess.run", side_effect=run_fake) as command,
    ):
        tasks = client.task.list(hydrate=True)
        assert [task.id for task in tasks] == ids
        assert [task.service_id for task in tasks] == owners
        assert command.call_count == 6

    assert service_cli.resolves() == [[identity[:12] for identity in services]]
    assert task_cli.lists() == [
        ["docker-test", "service", "ps", "--quiet", "--no-trunc", *services]
    ]
    assert task_cli.inspects() == [ids[:2], ids[2:4], ids[4:]]


def test_vanished_tasks_only_reprobe_the_affected_chunk() -> None:
    ids = [identity_of("task", index) for index in range(6)]
    cli = FakeCli("task", ids)
    cli.vanish_on_list = {ids[1], ids[2]}
    with fake_daemon(cli, chunk_size=3) as client:
        tasks = client.service.ps("api", hydrate=True)
        assert [task.id for task in tasks] == [ids[0], *ids[3:]]
        assert [task.name for task in tasks] == [
            "task-0000",
            "task-0003",
            "task-0004",
            "task-0005",
        ]

    assert cli.inspects() == [ids[:3], ids[:1], ids[1:3], ids[1:2], ids[2:3], ids[3:]]


@pytest.mark.parametrize(
    "failure", [*INSPECT_FAILURES, "unrequested", "mixed", "blank-line", "empty"]
)
def test_task_hydration_propagates_failures_without_bisection(failure: str) -> None:
    ids = [identity_of("task", index) for index in range(2)]
    cli = FakeCli("task", ids)
    missing = f"Error response from daemon: task {ids[0]} not found\n".encode()
    stderr = {
        "unrequested": b"Error response from daemon: task unrequested not found\n",
        "mixed": missing + b"permission denied\n",
        "blank-line": missing + b"\n",
        "empty": b"",
    }
    expected = INSPECT_FAILURES.get(failure, DockerException)
    with (
        fake_daemon(cli) as client,
        patch(
            "subprocess.run",
            side_effect=cli.failing({"inspect"}, stderr.get(failure, failure)),
        ),
        pytest.raises(expected) as caught,
    ):
        client.service.ps("api", hydrate=True)
    assert type(caught.value) is expected
    assert cli.inspects() == [ids]
