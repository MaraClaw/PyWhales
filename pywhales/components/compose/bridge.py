"""Compose Bridge commands: model conversion and transformation images.

Both command groups share the client's connection and execution policy and
need an installed Compose plugin. Nothing here probes the plugin version.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from pywhales._json import loads_compat
from pywhales.client_config import ClientConfig, Command, DockerCLICaller
from pywhales.components._json_output import JsonOutputParser
from pywhales.components.compose.models import ComposeTransformation
from pywhales.utils import (
    ValidPath,
    custom_parse_object_as,
    run,
    validate_timeout,
)


def _parse_transformations(output: str) -> builtins.list[ComposeTransformation]:
    decoded = loads_compat(output)
    return custom_parse_object_as(
        builtins.list[ComposeTransformation], [] if decoded is None else decoded
    )


class ComposeTransformationsCLI(DockerCLICaller):
    """Compose Bridge `transformations` commands for transformation images."""

    def list(self) -> builtins.list[ComposeTransformation]:
        """Return the transformation images available to Compose Bridge.

        Returns:
            One summary per transformation image, in CLI order; an empty list
            when none is installed.

        Raises:
            ParsingError: The output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command(
            [
                *self.docker_compose_cmd,
                "bridge",
                "transformations",
                "list",
                "--format",
                "json",
            ]
        )
        parser = JsonOutputParser(
            "compose bridge transformation list", self.client_config.raw_diagnostics
        )
        return parser.parse(_parse_transformations, run(full_cmd))

    def create(
        self,
        path: ValidPath,
        *,
        from_: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Create a new transformation from an existing one.

        The call waits for the local CLI, which can pull the source
        transformation image when it isn't available locally.

        Args:
            path: Directory receiving the new transformation, on the Python host.
            from_: Existing transformation to copy; `None` leaves the CLI default
                (the Kubernetes transformation).
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; a started image pull isn't guaranteed to stop.
        """
        validate_timeout(timeout)
        full_cmd = Command(
            [*self.docker_compose_cmd, "bridge", "transformations", "create"]
        )
        full_cmd.add_simple_arg("--from", from_)
        full_cmd.append(path)
        run(full_cmd, timeout=timeout)


class ComposeBridgeCLI(DockerCLICaller):
    """Compose Bridge commands, wired as `docker.compose.bridge`.

    Attributes:
        transformations: The `transformations` command group.
    """

    def __init__(self, client_config: ClientConfig) -> None:
        """Retain the config shared with the owning Compose command group.

        Args:
            client_config: Settings shared with the originating client.
        """
        super().__init__(client_config)
        self.transformations = ComposeTransformationsCLI(client_config)

    def convert(
        self,
        *,
        output: ValidPath | None = None,
        templates: ValidPath | None = None,
        transformations: str | Sequence[str] | None = None,
        yes: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Convert the Compose project into another model, such as Kubernetes.

        The local CLI inherits stdout and stderr, so progress and the overwrite
        prompt reach the caller's terminal. The call waits for the CLI, which
        can pull transformation images.

        Args:
            output: Directory receiving the result, on the Python host; `None`
                leaves the CLI default (`out`).
            templates: Directory containing transformation templates.
            transformations: One transformation image, or several applied in
                order. `None` leaves the CLI default Kubernetes transformation.
            yes: Assume "yes" to the output-directory overwrite prompt, which
                scripts and CI can't answer.
            timeout: Positive finite seconds from local CLI launch, or `None` to
                inherit the client default. Validated before the CLI is resolved.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: The local CLI exceeds its deadline and is killed and
                reaped; a started image pull isn't guaranteed to stop.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_compose_cmd, "bridge", "convert"])
        full_cmd.add_simple_arg("--output", output)
        full_cmd.add_simple_arg("--templates", templates)
        if transformations is not None:
            full_cmd.add_args_iterable_or_single("--transformation", transformations)
        full_cmd.add_flag("--yes", yes)
        run(full_cmd, capture_stdout=False, capture_stderr=False, timeout=timeout)
