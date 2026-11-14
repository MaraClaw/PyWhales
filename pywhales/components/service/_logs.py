"""Assemble the ``docker service logs`` command shared by service and task logs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from pywhales.client_config import Command
from pywhales.utils import format_time_arg


def logs_command(
    docker_cmd: Sequence[object],
    target: object,
    *,
    details: bool,
    since: datetime | timedelta | None,
    tail: int | None,
    timestamps: bool,
    follow: bool,
    raw: bool,
    task_ids: bool,
    resolve: bool,
    truncate: bool,
) -> Command:
    """Return a ``service logs`` command for one service or task reference.

    Args:
        docker_cmd: Executable and global arguments that start the command.
        target: Service or task name, ID, or handle, appended last.
        details: Include extra details Docker attaches to log records.
        since: Lower time bound, or None to omit it.
        tail: Trailing line count, or None for Docker's default of all lines.
        timestamps: Include timestamps with log records.
        follow: Keep following new records instead of taking a snapshot.
        raw: Omit Docker's log formatting.
        task_ids: Include task IDs in formatted output.
        resolve: Resolve IDs to names in formatted output.
        truncate: Allow Docker to truncate IDs in formatted output.

    Returns:
        The command, without an inspection preflight or launch.
    """
    full_cmd = Command([*docker_cmd, "service", "logs"])
    full_cmd.add_flag("--details", details)
    full_cmd.add_simple_arg("--since", format_time_arg(since))
    full_cmd.add_simple_arg("--tail", tail)
    full_cmd.add_flag("--timestamps", timestamps)
    full_cmd.add_flag("--follow", follow)
    full_cmd.add_flag("--raw", raw)
    full_cmd.add_flag("--no-task-ids", not task_ids)
    full_cmd.add_flag("--no-resolve", not resolve)
    full_cmd.add_flag("--no-trunc", not truncate)
    full_cmd.append(target)
    return full_cmd
