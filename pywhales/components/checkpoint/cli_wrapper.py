"""Checkpoint commands of the Docker CLI on the selected daemon.

Docker checkpoints use CRIU and require a daemon with experimental features
enabled; without them every command fails with a `DockerException`.
"""

from __future__ import annotations

import builtins

from pywhales.client_config import Command, DockerCLICaller
from pywhales.components._hydration import release_pins
from pywhales.components.container.cli_wrapper import ValidContainer
from pywhales.utils import ValidPath, run, validate_timeout


class CheckpointCLI(DockerCLICaller):
    """Checkpoint commands using the selected Docker daemon and client policy."""

    def create(
        self,
        container: ValidContainer,
        checkpoint: str,
        *,
        checkpoint_dir: ValidPath | None = None,
        leave_running: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Create a checkpoint from a running container.

        Without `leave_running` the daemon stops the container once the
        checkpoint is written. The call waits for the local checkpoint CLI.

        Args:
            container: The running container to checkpoint.
            checkpoint: Name of the checkpoint to create.
            checkpoint_dir: Custom checkpoint storage directory on the daemon
                host; `None` uses the daemon's default.
            leave_running: Leave the container running after the checkpoint.
            timeout: Positive finite seconds from local checkpoint CLI launch, or
                `None` to inherit the configured default. Validated before the
                CLI is resolved or launched. Expiry kills and reaps only the
                local CLI.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local checkpoint CLI exceeds its deadline.
            NoSuchContainer: The container does not exist.
            DockerException: The daemon rejects the checkpoint, for example
                because experimental features are disabled.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "checkpoint", "create"])
        full_cmd.add_simple_arg("--checkpoint-dir", checkpoint_dir)
        full_cmd.add_flag("--leave-running", leave_running)
        full_cmd.extend([container, checkpoint])
        try:
            run(full_cmd, timeout=timeout)
        finally:
            release_pins([container])

    def list(
        self,
        container: ValidContainer,
        *,
        checkpoint_dir: ValidPath | None = None,
    ) -> builtins.list[str]:
        """List the checkpoint names of a container.

        Args:
            container: The container whose checkpoints are listed.
            checkpoint_dir: Custom checkpoint storage directory on the daemon
                host; `None` uses the daemon's default.

        Returns:
            Checkpoint names in CLI order, without the table heading.

        Raises:
            NoSuchContainer: The container does not exist.
            DockerException: The daemon rejects the request, for example because
                experimental features are disabled.
        """
        full_cmd = Command([*self.docker_cmd, "checkpoint", "ls"])
        full_cmd.add_simple_arg("--checkpoint-dir", checkpoint_dir)
        full_cmd.append(container)
        lines = run(full_cmd).splitlines()[1:]
        return [line.strip() for line in lines if line.strip()]

    def remove(
        self,
        container: ValidContainer,
        checkpoint: str,
        *,
        checkpoint_dir: ValidPath | None = None,
    ) -> None:
        """Remove a checkpoint of a container.

        Args:
            container: The container that owns the checkpoint.
            checkpoint: Name of the checkpoint to remove.
            checkpoint_dir: Custom checkpoint storage directory on the daemon
                host; `None` uses the daemon's default.

        Raises:
            NoSuchContainer: The container does not exist.
            DockerException: The daemon rejects the request, for example because
                the checkpoint is missing or experimental features are disabled.
        """
        full_cmd = Command([*self.docker_cmd, "checkpoint", "rm"])
        full_cmd.add_simple_arg("--checkpoint-dir", checkpoint_dir)
        full_cmd.extend([container, checkpoint])
        run(full_cmd)
