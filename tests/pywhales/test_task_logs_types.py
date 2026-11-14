"""Static task log return contracts, checked by ty."""

from datetime import datetime, timedelta
from typing import assert_type

from pywhales import DockerClient
from pywhales.components.task.cli_wrapper import Task
from pywhales.utils import ProcessStream


def _check_task_logs(client: DockerClient, task: Task, stream: bool) -> None:
    assert_type(client.task.logs("task"), str)
    assert_type(client.task.logs(task, tail=3, timeout=1.0), str)
    assert_type(
        client.task.logs(
            "task",
            True,
            datetime(2024, 1, 2),
            1,
            True,
            False,
            True,
            False,
            False,
            False,
            task_ids=False,
            timeout=2.0,
        ),
        str,
    )
    assert_type(client.task.logs("task", since=timedelta(minutes=1)), str)
    assert_type(client.task.logs("task", stream=True), ProcessStream)
    assert_type(
        client.task.logs(task, follow=True, stream=True, timeout=1.0), ProcessStream
    )
    assert_type(client.task.logs("task", stream=stream), str | ProcessStream)
