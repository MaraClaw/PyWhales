"""Daemon-volume handles and filesystem transfers using temporary Docker resources."""

from __future__ import annotations

import builtins
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import (
    Literal,
    Self,
    overload,
    override,
)

from pydantic import JsonValue

import pywhales.components.buildx.cli_wrapper
import pywhales.components.container.cli_wrapper
import pywhales.components.image.cli_wrapper
from pywhales._execution_policy import execution_timeout
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ParsingError,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.volume.models import VolumeInspectResult
from pywhales.enums import ResourceScope, ResourceScopeInput
from pywhales.exceptions import NoSuchContainer, NoSuchImage, NoSuchVolume
from pywhales.test_utils import random_name
from pywhales.utils import (
    ValidPath,
    run,
    to_list,
    validate_timeout,
)

type VolumeListFilter = (
    tuple[Literal["driver"], str]
    | tuple[Literal["opt"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["label!"], str]
    | tuple[Literal["name"], str]
    | tuple[Literal["scope"], ResourceScopeInput]
    | tuple[Literal["after", "since"], str]
    | tuple[Literal["until"], str]
    | tuple[Literal["dangling"], str]
)


class Volume(ReloadableObjectFromJson[VolumeInspectResult]):
    """Reloadable Docker volume identified by name.

    Context-manager exit attempts removal, ignoring an already absent volume.
    Other cleanup failures are grouped with any active body exception.
    """

    def __init__(
        self, client_config: ClientConfig, reference: str, is_immutable_id: bool = False
    ) -> None:
        super().__init__(client_config, "name", reference, is_immutable_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with resource_cleanup(exc_val, "Volume body and cleanup both failed"):
            try:
                self.remove()
            except NoSuchVolume:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "volume", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> VolumeInspectResult:
        return VolumeInspectResult.model_validate(json_object)

    @property
    def name(self) -> str:
        return self._get_immutable_id()

    @property
    def driver(self) -> str | None:
        return self._get_inspect_result().driver

    @property
    def mountpoint(self) -> Path | None:
        return self._get_inspect_result().mountpoint

    @property
    def created_at(self) -> datetime | None:
        return self._get_inspect_result().created_at

    @property
    def status(self) -> dict[str, JsonValue] | None:
        return self._get_inspect_result().status

    @property
    def labels(self) -> dict[str, str] | None:
        return self._get_inspect_result().labels

    @property
    def scope(self) -> ResourceScope | str | None:
        return self._get_inspect_result().scope

    @property
    def options(self) -> dict[str, str] | None:
        return self._get_inspect_result().options

    @override
    def __repr__(self) -> str:
        return f"pywhales.Volume(name='{self.name}', driver={self.driver})"

    def remove(self) -> None:
        """Remove this volume through the configured Docker CLI."""
        VolumeCLI(self.client_config).remove(self)

    def clone(
        self,
        new_volume_name: str | None = None,
        driver: str | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        options: Mapping[str, str] = MappingProxyType({}),
        *,
        timeout: float | None = None,
    ) -> Volume:
        """Copy this volume's contents into a new or existing destination volume.

        See the [`docker.volume.clone`](../sub-commands/volume.md#clone) command for
        information about the arguments.
        """
        return VolumeCLI(self.client_config).clone(
            self, new_volume_name, driver, labels, options, timeout=timeout
        )

    def exists(self) -> bool:
        """Return whether this volume can still be inspected."""
        return VolumeCLI(self.client_config).exists(self.name)


ValidVolume = Volume | str
VolumePath = tuple[Volume | str, ValidPath]


class VolumeCLI(DockerCLICaller):
    """Volume commands whose copy helpers additionally require Buildx support."""

    def create(
        self,
        volume_name: str | None = None,
        driver: str | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        options: Mapping[str, str] = MappingProxyType({}),
    ) -> Volume:
        """Create and inspect a Docker volume.

        Args:
            volume_name: Requested name, or None to let Docker generate one.
            driver: Volume driver, or None to use Docker's default.
            labels: Metadata labels; the default adds none.
            options: Driver-specific options; the default adds none.

        Returns:
            An inspected handle to the volume Docker created or returned.
        """
        full_cmd = Command([*self.docker_cmd, "volume", "create"])

        full_cmd.add_simple_arg("--driver", driver)
        for key, value in labels.items():
            full_cmd += ["--label", f"{key}={value}"]
        for key, value in options.items():
            full_cmd += ["--opt", f"{key}={value}"]

        if volume_name is not None:
            full_cmd.append(volume_name)

        return Volume(self.client_config, run(full_cmd))

    @overload
    def inspect(self, x: str) -> Volume: ...

    @overload
    def inspect(self, x: Iterable[str]) -> builtins.list[Volume]: ...

    def inspect(self, x: str | Iterable[str]) -> Volume | builtins.list[Volume]:
        """Inspect one or more Docker volumes by name.

        Args:
            x: One volume name or an iterable of names. An empty iterable returns
                without invoking the CLI.

        Returns:
            One handle for a string, otherwise handles in request order with
            duplicates retained as distinct handles.

        Raises:
            ParsingError: If a batch response has invalid shape, inconsistent
                duplicate snapshots, or names that do not match the requests.
        """
        if isinstance(x, str):
            return Volume(self.client_config, x)
        references = list(x)
        if not references:
            return []
        json_str = run([*self.docker_cmd, "volume", "inspect", *references])
        parser = Volume(self.client_config, references[0], is_immutable_id=True)
        snapshots = parser._parse_inspect_results(json_str, references)
        volumes = []
        for reference, snapshot in zip(references, snapshots, strict=True):
            if snapshot.name is None or snapshot.name != reference:
                raise ParsingError(
                    f"Volume inspect identity does not match {reference!r}"
                    if self.client_config.raw_diagnostics
                    else "Volume inspect identity does not match the requested reference"
                )
            volume = Volume(self.client_config, snapshot.name, is_immutable_id=True)
            volume._set_inspect_result(snapshot)
            volumes.append(volume)
        return volumes

    def exists(self, x: str) -> bool:
        """Return whether a volume can be inspected.

        Args:
            x: Volume name to inspect.

        Returns:
            False when Docker reports NoSuchVolume; True on successful inspection.
            Other inspection failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchVolume:
            return False
        else:
            return True

    def list(self, filters: Iterable[VolumeListFilter] = ()) -> builtins.list[Volume]:
        """List Docker volumes.

        Args:
            filters: See the [Docker documentation page about filtering
                ](https://docs.docker.com/engine/reference/commandline/volume_ls/#filtering).
                An example `filters=[("dangling", "true"), ("driver", "local")]`.

        Returns:
            Volume handles in CLI output order.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "volume", "list", "--quiet"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        volumes_names = run(full_cmd).splitlines()

        return [
            Volume(self.client_config, x, is_immutable_id=True) for x in volumes_names
        ]

    def prune(
        self,
        filters: Iterable[VolumeListFilter] = (),
        all: bool = False,
        *,
        timeout: float | None = None,
    ) -> None:
        """Remove unused Docker volumes without a confirmation prompt.

        Args:
            filters: See the [Docker documentation page about filtering
                ](https://docs.docker.com/engine/reference/commandline/volume_ls/#filtering).
                An example `filters=[("dangling", "true"), ("driver", "local")]`.
            all: Remove all unused volumes, not just anonymous ones.
            timeout: Positive finite seconds from local CLI launch; `None` inherits
                the client default. Expiry cannot undo daemon-side removal.
        """
        validate_timeout(timeout)
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "volume", "prune", "--force"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        run(full_cmd, timeout=timeout)

    def remove(self, x: ValidVolume | Iterable[ValidVolume]) -> None:
        """Remove one or more volumes.

        Args:
            x: A volume or an iterable of volumes. An empty selection means
                nothing is done.
        """
        volumes = to_list(x)
        if not volumes:
            return
        full_cmd = Command([*self.docker_cmd, "volume", "remove"])
        for v in volumes:
            full_cmd.append(str(v))

        run(full_cmd)

    def clone(
        self,
        source: ValidVolume,
        new_volume_name: str | None = None,
        driver: str | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        options: Mapping[str, str] = MappingProxyType({}),
        *,
        timeout: float | None = None,
    ) -> Volume:
        """Copy a volume's data into a newly created or existing named volume.

        Data passes through a temporary directory on the Python host using two
        copy operations. Buildx and the selected Docker daemon are required.
        A failure does not remove the destination volume.

        Args:
            source: Source volume name or handle.
            new_volume_name: Destination name, or None to let Docker generate one.
            driver: Destination volume driver, or None to use Docker's default.
            labels: Destination metadata labels; the default adds none.
            options: Destination driver options; the default adds none.
            timeout: Positive finite seconds per local CLI launch across the
                destination creation and inspection and both copies, or `None`
                to inherit the configured default. Validated before any launch.

        Returns:
            The destination volume handle after both copies complete.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: A local CLI launch exceeds its deadline.
        """
        validate_timeout(timeout)
        with execution_timeout(self.client_config, timeout):
            new_volume = self.create(new_volume_name, driver, labels, options)
            with tempfile.TemporaryDirectory() as temp_dir:
                self.copy((source, "."), temp_dir, timeout=timeout)
                self.copy(str(temp_dir) + "/.", (new_volume, ""), timeout=timeout)
        return new_volume

    def copy(
        self,
        source: ValidPath | VolumePath,
        destination: ValidPath | VolumePath,
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy files/folders between a volume and the local filesystem.

        This builds a temporary image with Buildx and creates a container mounting
        the volume. The helper container and image are removed on success and on
        failure; if removal also fails, both errors are kept in a
        `BaseExceptionGroup`. Use one volume endpoint and one local endpoint,
        with the local path resolved on the Python host.

        Args:
            source: If `source` is a directory/file inside a Docker volume,
                a tuple `(my_volume, path_in_volume)` must be provided. The volume
                can be a `pywhales.Volume` or a volume name as `str`. The path
                can be a `pathlib.Path` or a `str`. If `source` is  a local directory,
                a `pathlib.Path` or `str` should be provided. End the source path with
                `/.` if you want to copy the directory content in another directory.
            destination: Destination path, using the same endpoint notation.
            timeout: Positive finite seconds per local CLI launch across the
                image build, helper container creation, copy, and removals, or
                `None` to inherit the configured default. Validated before any
                launch.

        Raises:
            ValueError: If neither endpoint is a volume/path tuple, or `timeout`
                is not positive and finite.
            DockerTimeout: A local CLI launch exceeds its deadline.
        """
        validate_timeout(timeout)
        if isinstance(source, tuple):
            volume_name = str(source[0])
        elif isinstance(destination, tuple):
            volume_name = str(destination[0])
        else:
            raise ValueError("source or destination should be a tuple.")

        images = pywhales.components.image.cli_wrapper.ImageCLI(self.client_config)
        containers = pywhales.components.container.cli_wrapper.ContainerCLI(
            self.client_config
        )
        buildx = pywhales.components.buildx.cli_wrapper.BuildxCLI(self.client_config)
        image_name = random_name()
        container_name = random_name()
        volume_in_container = Path("/volume")
        failure: BaseException | None = None
        with execution_timeout(self.client_config, timeout):
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_dir = Path(temp_dir)
                    content = "FROM scratch\nCOPY Dockerfile /\nCMD /Dockerfile"
                    (temp_dir / "Dockerfile").write_text(content)
                    dummy_image = buildx.build(
                        temp_dir,
                        tags=image_name,
                        progress=False,
                        load=True,
                        timeout=timeout,
                    )
                assert isinstance(
                    dummy_image, pywhales.components.image.cli_wrapper.Image
                )
                try:
                    dummy_container = containers.create(
                        dummy_image,
                        name=container_name,
                        volumes=[(volume_name, volume_in_container)],
                    )
                    if isinstance(source, tuple):
                        target = (
                            (str(destination[0]), destination[1])
                            if isinstance(destination, tuple)
                            else destination
                        )
                        containers.copy(
                            (
                                dummy_container,
                                os.path.join(volume_in_container, source[1]),
                            ),
                            target,
                            timeout=timeout,
                        )
                    elif isinstance(destination, tuple):
                        containers.copy(
                            source,
                            (
                                dummy_container,
                                os.path.join(volume_in_container, destination[1]),
                            ),
                            timeout=timeout,
                        )
                except BaseException as error:
                    failure = error
                    raise
                finally:
                    with (
                        resource_cleanup(
                            failure, "Volume copy and helper container cleanup failed"
                        ),
                        suppress(NoSuchContainer),
                    ):
                        containers.remove(container_name)
            except BaseException as error:
                failure = error
                raise
            finally:
                with (
                    resource_cleanup(
                        failure, "Volume copy and helper image cleanup failed"
                    ),
                    suppress(NoSuchImage),
                ):
                    images.remove(image_name)


VolumeDefinition = (
    tuple[Volume | ValidPath, ValidPath] | tuple[Volume | ValidPath, ValidPath, str]
)
