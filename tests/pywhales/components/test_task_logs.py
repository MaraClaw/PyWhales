"""Task logs launched through a recording fake CLI, without a daemon."""

from datetime import timedelta
from pathlib import Path

import pytest

from pywhales import DockerClient
from pywhales.components.task.cli_wrapper import Task
from pywhales.exceptions import DockerException, DockerTimeout
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

TASK_ID = "abcde" * 5


@pytest.fixture
def fake(tmp_path: Path) -> tuple[DockerClient, Path]:
    return make_fake_cli(
        tmp_path, [Rule(match=("logs",), stdout="out line\n", stderr="err line\n")]
    )


def test_defaults_launch_service_logs_once_without_a_preflight(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    text = client.task.logs(TASK_ID)
    assert sorted(text.splitlines()) == ["err line", "out line"]
    assert calls(log) == [["service", "logs", TASK_ID]]


def test_every_option_reaches_argv_in_service_logs_order(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.task.logs(
        TASK_ID,
        True,
        timedelta(minutes=5),
        10,
        True,
        True,
        True,
        False,
        False,
        task_ids=False,
    )
    assert calls(log) == [
        [
            "service",
            "logs",
            "--details",
            "--since",
            "300.0s",
            "--tail",
            "10",
            "--timestamps",
            "--follow",
            "--raw",
            "--no-task-ids",
            "--no-resolve",
            "--no-trunc",
            TASK_ID,
        ]
    ]


def test_tail_zero_is_forwarded(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    client.task.logs(TASK_ID, tail=0)
    assert calls(log) == [["service", "logs", "--tail", "0", TASK_ID]]


def test_task_handle_targets_its_id(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    task = Task(client.client_config, TASK_ID, is_immutable_id=True)
    client.task.logs(task)
    assert calls(log) == [["service", "logs", TASK_ID]]


def test_stream_returns_an_owned_stream_of_labelled_records(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    with client.task.logs(TASK_ID, stream=True) as stream:
        records = list(stream)
    assert set(records) == {("stdout", b"out line\n"), ("stderr", b"err line\n")}
    assert calls(log) == [["service", "logs", TASK_ID]]


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
@pytest.mark.parametrize("stream", [False, True])
def test_invalid_timeout_precedes_executable_lookup(
    timeout: float, stream: bool
) -> None:
    client = DockerClient(client_call=["missing-pywhales-test-executable"])
    with pytest.raises(ValueError):
        client.task.logs(TASK_ID, stream=stream, timeout=timeout)


def test_cli_failure_is_raised_once(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(
                match=("logs",),
                stderr="Error response from daemon: not found\n",
                exit_code=1,
            )
        ],
    )
    with pytest.raises(DockerException) as failure:
        client.task.logs(TASK_ID)
    assert failure.value.return_code == 1
    assert calls(log) == [["service", "logs", TASK_ID]]


def test_expired_deadline_kills_the_local_cli(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("logs",), stdout="partial\n", sleep=30)]
    )
    with pytest.raises(DockerTimeout):
        client.task.logs(TASK_ID, timeout=0.5)
