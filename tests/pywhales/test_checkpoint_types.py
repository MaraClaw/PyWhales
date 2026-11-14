"""Static contracts for the checkpoint command group; checked by ty, not pytest."""

from pathlib import Path
from typing import assert_type

from pywhales import Container, DockerClient
from pywhales.components.checkpoint.cli_wrapper import CheckpointCLI


def _check_checkpoint(client: DockerClient, container: Container) -> None:
    assert_type(client.checkpoint, CheckpointCLI)
    checkpoints = client.checkpoint
    assert_type(checkpoints.create("web", "cp1"), None)
    assert_type(
        checkpoints.create(
            container,
            "cp1",
            checkpoint_dir=Path("/var/cp"),
            leave_running=True,
            timeout=5,
        ),
        None,
    )
    assert_type(checkpoints.list("web"), list[str])
    assert_type(checkpoints.list(container, checkpoint_dir="/var/cp"), list[str])
    assert_type(checkpoints.remove("web", "cp1"), None)
    assert_type(checkpoints.remove(container, "cp1", checkpoint_dir=Path("x")), None)
