"""Local image handles, registry transfers, and Docker archive operations.

Commands use the configured Docker CLI and selected daemon. Progress streams and
archive-byte iterators have different startup and cleanup contracts.
"""

from __future__ import annotations

import builtins
import os
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import nullcontext, suppress
from datetime import datetime
from pathlib import Path
from subprocess import PIPE, Popen
from time import monotonic
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Literal,
    Never,
    Self,
    overload,
    override,
)

from pydantic import JsonValue

import pywhales.components.buildx.cli_wrapper
from pywhales._archive_deadline import ArchiveDeadline
from pywhales._environment import finalize_environment, resolve_environment
from pywhales._execution_policy import (
    ExecutionPolicy,
    execution_timeout,
    get_execution_policy,
)
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ParsingError,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.container.cli_wrapper import (
    ContainerCLI,
    ContainerConfig,
)
from pywhales.components.image.models import (
    ImageGraphDriver,
    ImageInspectResult,
    ImageRootFS,
)
from pywhales.components.image.streams import (
    EmptyImageStream,
    ImageStream,
    MultiImageStream,
    _EmptyImageStream,
    map_in_contexts,
)
from pywhales.exceptions import DockerException, NoSuchImage
from pywhales.telemetry import OperationFamily
from pywhales.telemetry._lifecycle import ExecutionTelemetry
from pywhales.utils import (
    MappedProcessStream,
    ValidPath,
    run,
    stream_stdout_and_stderr,
    to_list,
    validate_timeout,
)

if TYPE_CHECKING:
    from pywhales._observation import ObservationState

type ImageListFilter = (
    tuple[Literal["id"], str]
    | tuple[Literal["reference"], str]
    | tuple[Literal["digest"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["label!"], str]
    | tuple[Literal["before"], str]
    | tuple[Literal["after", "since"], str]
    | tuple[Literal["until"], str]
    | tuple[Literal["dangling"], str]
    | tuple[Literal["intermediate"], str]
    | tuple[Literal["manifest"], str]
    | tuple[Literal["readonly"], str]
)


def _select_archive_timeout(
    policy: ExecutionPolicy, timeout: float | None
) -> float | None:
    """Return the validated seconds bounding one direct archive launch.

    Args:
        policy: Execution policy snapshotted for the launch.
        timeout: Per-call seconds; `None` selects the policy default.

    Returns:
        The explicit `timeout` when given, otherwise `policy.default_timeout`;
        `None` means no deadline.

    Raises:
        ValueError: The selected value is boolean, nonfinite, zero, or negative.
    """
    selected = policy.default_timeout if timeout is None else timeout
    validate_timeout(selected)
    return selected


def _prepare_archive_launch(
    full_cmd: Sequence[object], timeout: float | None, family: OperationFamily
) -> tuple[
    ExecutionPolicy,
    float | None,
    list[str],
    dict[str, str],
    ObservationState | None,
]:
    """Snapshot everything one direct archive launch needs, before any process.

    The observation active when this runs is the launch's scope: for a lazy save
    that is the first iteration, never the call that created the generator. Like
    a captured run, a stopped scope or an oversized argv or environment is
    rejected here, before any input is consumed or telemetry sees a launch.

    Args:
        full_cmd: Complete archive command; the first item may carry the policy.
        timeout: Per-call seconds, or `None` to select the policy default.
        family: Fixed family of the direct archive operation.

    Returns:
        The execution policy, selected timeout, stringified command, finalized
        child environment, and the launch-time observation (or `None`).

    Raises:
        ValueError: The selected timeout is boolean, nonfinite, zero, or negative.
        ObservationCancelled: The active observation was cancelled.
        ObservationTimeout: The active observation deadline has passed.
        ObservationLimitError: The active observation rejects the argv or
            environment.
    """
    from pywhales._observation import get_observation
    from pywhales._scoped_launch import check_scoped_launch, environment_limit

    observation = get_observation()
    observed = observation is not None
    with environment_limit(observed):
        policy = get_execution_policy(full_cmd)
    selected_timeout = _select_archive_timeout(policy, timeout)
    with environment_limit(observed):
        environment = resolve_environment(
            policy.environment, frozen=policy.frozen_environment
        )
    command = [str(argument) for argument in full_cmd]
    environment = finalize_environment(environment, command)
    if observation is not None:
        with ExecutionTelemetry(policy.telemetry_observer, family).preflight():
            check_scoped_launch(command, environment, observation)
    return policy, selected_timeout, command, environment, observation


def _launch_archive(
    command: list[str],
    environment: Mapping[str, str],
    observation: ObservationState | None,
    *,
    stdin: int | None = None,
    stderr: int | None = None,
) -> Popen[bytes]:
    """Start an unbuffered archive child with a piped stdout.

    An operating-system argument-size refusal is reported as an observation
    limit only when the launch is observed.
    """
    from pywhales._scoped_launch import native_argv_limit

    with nullcontext() if observation is None else native_argv_limit(command):
        return Popen(
            command, stdin=stdin, stdout=PIPE, stderr=stderr, env=environment, bufsize=0
        )


def _own_archive(
    process: Popen[bytes],
    command: list[str],
    deadline: float | None,
    *,
    raw: bool,
    observation: ObservationState | None,
) -> ArchiveDeadline:
    """Start the archive watchdog, passing the scope along only when observed."""
    if observation is None:
        return ArchiveDeadline(process, command, deadline, raw=raw)
    return ArchiveDeadline(process, command, deadline, raw=raw, observation=observation)


class Image(ReloadableObjectFromJson[ImageInspectResult]):
    """Reloadable local image handle retaining its originating configuration.

    Inspection-backed properties may refresh through the Docker CLI. Using the
    handle as a context manager opts into forced image removal on exit, even for
    a pre-existing image; an already missing image is tolerated during cleanup.
    """

    def __init__(
        self, client_config: ClientConfig, reference: str, is_immutable_id: bool = False
    ) -> None:
        super().__init__(client_config, "id", reference, is_immutable_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with resource_cleanup(exc_val, "Image body and cleanup both failed"):
            try:
                self.remove(force=True)
            except NoSuchImage:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "image", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: Mapping[str, JsonValue]
    ) -> ImageInspectResult:
        return ImageInspectResult.model_validate(json_object)

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def repo_tags(self) -> list[str] | None:
        return self._get_inspect_result().repo_tags

    @property
    def repo_digests(self) -> list[str] | None:
        return self._get_inspect_result().repo_digests

    @property
    def parent(self) -> str | None:
        return self._get_inspect_result().parent

    @property
    def comment(self) -> str | None:
        return self._get_inspect_result().comment

    @property
    def created(self) -> datetime | None:
        return self._get_inspect_result().created

    @property
    def container(self) -> str | None:
        return self._get_inspect_result().container

    @property
    def container_config(
        self,
    ) -> ContainerConfig | None:
        return self._get_inspect_result().container_config

    @property
    def docker_version(self) -> str | None:
        return self._get_inspect_result().docker_version

    @property
    def author(self) -> str | None:
        return self._get_inspect_result().author

    @property
    def config(
        self,
    ) -> ContainerConfig | None:
        return self._get_inspect_result().config

    @property
    def architecture(self) -> str | None:
        return self._get_inspect_result().architecture

    @property
    def os(self) -> str | None:
        return self._get_inspect_result().os

    @property
    def os_version(self) -> str | None:
        return self._get_inspect_result().os_version

    @property
    def variant(self) -> str | None:
        return self._get_inspect_result().variant

    @property
    def size(self) -> int | None:
        return self._get_inspect_result().size

    @property
    def virtual_size(self) -> int | None:
        return self._get_inspect_result().virtual_size

    @property
    def graph_driver(self) -> ImageGraphDriver | None:
        return self._get_inspect_result().graph_driver

    @property
    def root_fs(self) -> ImageRootFS | None:
        return self._get_inspect_result().root_fs

    @property
    def metadata(self) -> Mapping[str, str] | None:
        return self._get_inspect_result().metadata

    @override
    def __repr__(self) -> str:
        return f"pywhales.Image(id='{self.id[:20]}', tags={self.repo_tags})"

    def remove(self, force: bool = False, prune: bool = True) -> None:
        """Remove this Docker image.

        See the [`docker.image.remove`](../sub-commands/image.md#pywhales.components.image.cli_wrapper.ImageCLI.remove) command for
        information about the arguments.
        """
        ImageCLI(self.client_config).remove(self, force, prune)

    @overload
    def save(
        self, output: None = None, *, timeout: float | None = None
    ) -> Iterator[bytes]: ...

    @overload
    def save(self, output: ValidPath, *, timeout: float | None = None) -> None: ...

    @overload
    def save(
        self, output: ValidPath | None, *, timeout: float | None = None
    ) -> Iterator[bytes] | None: ...

    def save(
        self, output: ValidPath | None = None, *, timeout: float | None = None
    ) -> Iterator[bytes] | None:
        """Save this image as a Docker tar archive.

        See the [`docker.image.save`](../sub-commands/image.md#pywhales.components.image.cli_wrapper.ImageCLI.save) command for
        information about the arguments.

        Args:
            output: Archive path, or `None` to return archive bytes lazily.
            timeout: Positive finite seconds from each local CLI launch (the
                inspection and the save), replacing the client default; `None`
                inherits it. Each launch gets its own deadline, and a lazy
                archive starts its save clock on first iteration.

        Returns:
            An archive-byte generator when `output=None`; otherwise `None` after
            the save CLI completes. Inspection occurs during this call; the
            generator starts its save subprocess on first iteration. Close it
            when abandoning consumption to leave its subprocess context; cleanup
            can wait for the CLI and is not ProcessStream cancellation.

        Raises:
            ValueError: `timeout` is not positive and finite. Booleans are
                rejected before inspection.
            DockerTimeout: The save CLI exceeds its deadline; it is killed and
                reaped without guaranteeing that daemon-side work stops.
            ObservationTimeout: The observation active when a lazy archive first
                iterates (or when a path save runs) expired first.
            ObservationCancelled: A token of that observation was set.
        """
        return ImageCLI(self.client_config).save(self, output, timeout=timeout)

    def tag(self, new_tag: str) -> None:
        """Add a tag to a Docker image.

        See the [`docker.image.tag`](../sub-commands/image.md#pywhales.components.image.cli_wrapper.ImageCLI.tag) command for
        information about the arguments.
        """
        return ImageCLI(self.client_config).tag(self, new_tag)

    def copy_from(
        self,
        path_in_image: ValidPath,
        destination: ValidPath,
        pull: str = "missing",
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy files from this image to the local filesystem.

        The copy uses a temporary container that is removed on context exit.

        Args:
            path_in_image: Source path inside the image.
            destination: Local destination path.
            pull: Image acquisition policy, forwarded to container creation;
                defaults to `"missing"`.
            timeout: Positive finite seconds from each local CLI launch the
                copy makes, replacing the client default; `None` inherits it.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: A launched local CLI exceeds its deadline.
        """
        return ImageCLI(self.client_config).copy_from(
            self, path_in_image, destination, pull, timeout=timeout
        )

    def copy_to(
        self,
        local_path: ValidPath,
        path_in_image: ValidPath,
        new_tag: str | None = None,
        pull: str = "missing",
        *,
        timeout: float | None = None,
    ) -> Image:
        """Copy local files into a temporary container and commit a new image.

        The temporary container is removed on context exit. The resulting image
        remains available; the original image is not modified.

        Args:
            local_path: Local source path.
            path_in_image: Destination path inside the new image.
            new_tag: Optional tag applied when committing; `None` omits a tag.
            pull: Image acquisition policy, forwarded to container creation;
                defaults to `"missing"`.
            timeout: Positive finite seconds from each local CLI launch the
                copy makes, replacing the client default; `None` inherits it.

        Returns:
            The committed image using the originating client configuration.

        Raises:
            ValueError: `timeout` is not positive and finite.
            DockerTimeout: A launched local CLI exceeds its deadline.
        """
        return ImageCLI(self.client_config).copy_to(
            self, local_path, path_in_image, new_tag, pull, timeout=timeout
        )

    def exists(self) -> bool:
        """Return whether this image's immutable ID still exists in the daemon.

        Note that you might have done `docker.image.remove("some_tag")` and the image
        might still exist because PyWhales references images by ID, not by tag.

        Only a missing-image failure becomes `False`; other CLI failures propagate.
        """
        return ImageCLI(self.client_config).exists(self.id)


ValidImage = str | Image


class ImageCLI(DockerCLICaller):
    """Image commands sharing client configuration and Buildx build behavior."""

    def __init__(self, client_config: ClientConfig) -> None:
        super().__init__(client_config)
        self.build = pywhales.components.buildx.cli_wrapper.BuildxCLI(
            client_config
        ).build

    def history(self) -> Never:
        """Raise NotImplementedError because image history is not implemented.

        Raises:
            NotImplementedError: This operation has no implementation.
        """
        raise NotImplementedError

    def import_(
        self,
        source: ValidPath,
        tag: str | None = None,
        changes: Iterable[str] = (),
        message: str | None = None,
        platform: str | None = None,
    ) -> Image:
        """Import a tarball's contents as a new filesystem image.

        Alias: `docker.import_(...)`

        Args:
            source: Archive path or source accepted by `docker image import`.
            tag: Optional repository tag for the imported image.
            changes: Apply Dockerfile instruction to the created image
            message: Set commit message for imported image
            platform: Set platform if server is multi-platform capable

        Returns:
            The imported image, inspected using the originating configuration.
        """
        full_cmd = Command([*self.docker_cmd, "image", "import"])
        full_cmd.add_args_iterable("--change", changes)
        full_cmd.add_simple_arg("--message", message)
        full_cmd.add_simple_arg("--platform", platform)
        full_cmd.append(source)
        if tag is not None:
            full_cmd.append(tag)
        return Image(self.client_config, run(full_cmd))

    @overload
    def inspect(self, x: str) -> Image: ...

    @overload
    def inspect(self, x: Iterable[str]) -> builtins.list[Image]: ...

    def inspect(self, x: str | Iterable[str]) -> Image | builtins.list[Image]:
        """Inspect one or more local image references and return image handles.

        Iterable input uses one batch inspection and validates every snapshot's
        identity before returning handles with seeded inspection caches.

        Args:
            x: A reference, or references in the desired result order. Tags,
                digests, and IDs are accepted; an empty iterable skips the CLI.

        Returns:
            One image for a string, or a list preserving iterable order and
            duplicates as separate handles. Empty iterable input returns `[]`.

        Raises:
            NoSuchImage: A requested image does not exist in the selected daemon.
            ParsingError: Batch data cannot be validated or associated with its
                requested image reference.
        """
        if isinstance(x, str):
            return Image(self.client_config, x)
        references = list(x)
        if not references:
            return []
        json_str = run([*self.docker_cmd, "image", "inspect", *references])
        parser = Image(self.client_config, references[0], is_immutable_id=True)
        snapshots = parser._parse_inspect_results(json_str, references)

        def normalized(reference: str) -> str:
            reference = reference.removeprefix("index.docker.io/").removeprefix(
                "docker.io/"
            )
            reference = reference.removeprefix("library/")
            if "@" in reference:
                repository, digest = reference.split("@", 1)
                if ":" in repository.rsplit("/", 1)[-1]:
                    repository = repository.rsplit(":", 1)[0]
                return repository + "@" + digest
            if ":" not in reference.rsplit("/", 1)[-1]:
                return reference + ":latest"
            return reference

        images = []
        for reference, snapshot in zip(references, snapshots, strict=True):
            aliases = [*(snapshot.repo_tags or []), *(snapshot.repo_digests or [])]
            normalized_aliases = {normalized(alias) for alias in aliases}
            if snapshot.id is None or not (
                reference
                and (
                    snapshot.id.startswith(reference)
                    or snapshot.id.removeprefix("sha256:").startswith(reference)
                    or normalized(reference) in normalized_aliases
                )
            ):
                raise ParsingError(
                    f"Image inspect identity does not match {reference!r}"
                    if self.client_config.raw_diagnostics
                    else "Image inspect identity does not match the requested reference"
                )
            image = Image(self.client_config, snapshot.id, is_immutable_id=True)
            image._set_inspect_result(snapshot)
            images.append(image)
        return images

    def exists(self, x: str) -> bool:
        """Return whether a local image reference can be inspected.

        Args:
            x: Image tag, digest, or ID in the selected daemon.

        Returns:
            `False` only when inspection raises `NoSuchImage`; `True` on success.
            Other CLI failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchImage:
            return False
        else:
            return True

    def load(
        self,
        input: ValidPath | bytes | Iterator[bytes],
        quiet: bool = False,
        *,
        timeout: float | None = None,
    ) -> builtins.list[str]:
        """Load images eagerly from an archive path or archive bytes.

        Alias: `docker.load(...)`

        Path input consumes a process-backed progress stream before returning.
        Byte input is written to a local CLI's stdin and fully consumed during
        the call. These archive bytes are not progress-log records. The selected
        deadline (explicit `timeout`, otherwise the configured default) reaps
        the local CLI even if the input iterator stalls; an iterator's Python
        code is not interrupted. Inside `observation()` the load first checks
        cancellation and the argv and environment limits, before consuming any
        input, and the active scope's deadline and tokens reap the CLI the same way.

        Args:
            input: Archive path, complete archive bytes, or an iterator of archive
                byte chunks. Loading adds images and tags to the selected daemon.
            quiet: Pass the CLI's quiet flag. For path input, also suppress
                printing of decoded output records.
            timeout: Positive finite seconds from the load CLI's own launch,
                replacing the client default; `None` inherits it. It is
                validated before `input` is consumed.

        Returns:
            Image tags or IDs extracted from `Loaded image` output lines, in
            output order, for both paths and byte input.

        Raises:
            ValueError: `timeout` is not positive and finite. Booleans are
                rejected before any input is consumed or CLI is resolved.
            DockerTimeout: The load CLI exceeds its deadline; it is killed and
                reaped without guaranteeing that daemon-side work stops.
            ObservationTimeout: An active observation deadline passed first.
            ObservationCancelled: An active observation token was set.
            ObservationLimitError: An active observation rejects the argv or
                environment, before any CLI starts.
        """
        validate_timeout(timeout)
        full_cmd = Command([*self.docker_cmd, "image", "load"])

        if isinstance(input, (str, Path)):
            full_cmd += ["--input", str(input)]

        if quiet:
            full_cmd.append("--quiet")

        if isinstance(input, (str, Path)):
            all_tags = []
            for _, stream_bytes in stream_stdout_and_stderr(full_cmd, timeout=timeout):
                decoded = stream_bytes.decode()[:-1]
                if not quiet:
                    print(decoded)
                if "Loaded image" in decoded:
                    all_tags.append(decoded.split(" ")[-1])
            return all_tags

        input_chunks = (input,) if isinstance(input, bytes) else input
        stdout_lines = self._load_from_generator(full_cmd, input_chunks, timeout)
        all_tags = []
        for line in stdout_lines:
            if "Loaded image" in line:
                all_tags.append(line.split(" ")[-1])
        return all_tags

    def _load_from_generator(
        self,
        full_cmd: Sequence[object],
        input: Iterable[bytes],
        timeout: float | None,
    ) -> builtins.list[str]:
        policy, selected_timeout, command, environment, observation = (
            _prepare_archive_launch(full_cmd, timeout, "archive_load")
        )
        with ExecutionTelemetry(policy.telemetry_observer, "archive_load") as telemetry:
            telemetry.start()
            deadline = (
                None if selected_timeout is None else monotonic() + selected_timeout
            )
            with _launch_archive(
                command, environment, observation, stdin=PIPE
            ) as process:
                assert process.stdin is not None
                assert process.stdout is not None
                owner = _own_archive(
                    process,
                    command,
                    deadline,
                    raw=policy.raw_diagnostics,
                    observation=observation,
                )
                try:
                    for buffer_bytes in input:
                        owner.write(process.stdin, buffer_bytes)
                    owner.check()
                    process.stdin.close()
                    chunks = []
                    while chunk := owner.read(process.stdout):
                        chunks.append(chunk)
                    stdout = b"".join(chunks)
                    owner.check()
                    exit_code = process.wait()
                    owner.check()
                finally:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    process.wait()
                    owner.thread.join()
                    telemetry.return_code = process.returncode
                    telemetry.stdin_bytes = owner.stdin_bytes
                    telemetry.stdout_bytes = owner.stdout_bytes
                    telemetry.stderr_bytes = owner.stderr_bytes
            if exit_code != 0:
                raise DockerException(
                    command, exit_code, raw_diagnostics=policy.raw_diagnostics
                )
            return stdout.decode().splitlines()

    def list(
        self,
        repository_or_tag: str | None = None,
        filters: Iterable[ImageListFilter] = (),
        all: bool = False,
    ) -> builtins.list[Image]:
        """Return unique image IDs present in the selected Docker daemon.

        Alias: `docker.images()`

        Note that each image may have multiple tags.

        Args:
            repository_or_tag: Optional CLI repository or tag selector.
            filters: CLI filter pairs.
            all: Include intermediate images.

        Returns:
            One handle per distinct image ID, with unspecified order, retaining
            the originating client configuration.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command(
            [
                *self.docker_cmd,
                "image",
                "list",
                "--quiet",
                "--no-trunc",
            ]
        )
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        if repository_or_tag is not None:
            full_cmd.append(repository_or_tag)

        ids = run(full_cmd).splitlines()
        # the list of tags is bigger than the number of images. We uniquify
        ids = set(ids)

        return [Image(self.client_config, x, is_immutable_id=True) for x in ids]

    def prune(
        self,
        all: bool = False,
        filters: Iterable[ImageListFilter] = (),
        *,
        timeout: float | None = None,
    ) -> str:
        """Remove unused images without prompting.

        Args:
            all: Remove all unused images, not just dangling ones
            filters: Provide filter values (e.g. `[("until", "<timestamp>")]`)
            timeout: Positive finite seconds from local CLI launch; `None` inherits
                the client default. Expiry cannot undo daemon-side removal.

        Returns:
            The output of the CLI (the layers removed).
        """
        validate_timeout(timeout)
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "image", "prune", "--force"])
        full_cmd.add_flag("--all", all)
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )
        return run(full_cmd, timeout=timeout)

    @overload
    def pull(
        self,
        x: str,
        quiet: bool = False,
        stream_logs: Literal[False] = False,
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Image: ...

    @overload
    def pull(
        self,
        x: Iterable[str],
        quiet: bool = False,
        stream_logs: Literal[False] = False,
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> builtins.list[Image]: ...

    @overload
    def pull(
        self,
        x: str,
        quiet: bool = False,
        *,
        stream_logs: Literal[True],
        platform: str | None = None,
        timeout: float | None = None,
    ) -> ImageStream: ...

    @overload
    def pull(
        self,
        x: str,
        quiet: bool,
        stream_logs: Literal[True],
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> ImageStream: ...

    @overload
    def pull(
        self,
        x: Iterable[str],
        quiet: bool = False,
        *,
        stream_logs: Literal[True],
        platform: str | None = None,
        timeout: float | None = None,
    ) -> ImageStream | EmptyImageStream: ...

    @overload
    def pull(
        self,
        x: Iterable[str],
        quiet: bool,
        stream_logs: Literal[True],
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> ImageStream | EmptyImageStream: ...

    @overload
    def pull(
        self,
        x: str,
        quiet: bool = False,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Image | ImageStream: ...

    @overload
    def pull(
        self,
        x: Iterable[str],
        quiet: bool = False,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> builtins.list[Image] | ImageStream | EmptyImageStream: ...

    def pull(
        self,
        x: str | Iterable[str],
        quiet: bool = False,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Image | builtins.list[Image] | ImageStream | EmptyImageStream:
        """Pull one or more image references into the selected Docker daemon.

        Alias: `docker.pull(...)`

        Blocking transfers start during the call and use at most four workers
        for multiple distinct references. Streaming one unique reference starts
        its CLI before returning; streaming several starts up to four workers on
        first consumption, not on construction or context entry.

        Nonempty streams own their local CLIs. Exhaust them or use a context
        manager or `close()` on early exit; a bare loop break does not close them.
        Closing cancels local CLIs without undoing pulls or guaranteeing that
        daemon-side transfers stop. The selected image/general stream budget is
        snapshotted before consuming references and retained for queued children.

        Inside `observation()` one and several references behave alike: every
        pool task and every stream worker runs in its own copy of the calling
        context, so `quiet=False` raises the same `ValueError` for one image or
        many, and a stream group keeps the scope it was created in after the
        `with` block exits.

        Args:
            x: Image reference or iterable of references. Iterable references are
                deduplicated in first-seen order before selecting transfer mode.
            quiet: If you don't want to see the progress bars.
            stream_logs: Return owned `(image_reference, bytes)` progress records
                instead of waiting for transfers and returning image handles.
            platform: If you want to enforce a platform.
            timeout: Positive finite seconds from each transfer subprocess's own
                launch, replacing the client default; `None` inherits it. Image
                inspection and executor queueing do not consume this budget, and
                each concurrent transfer gets its own deadline.

        Returns:
            One image for a string, or an ordered list for iterable input when
            `stream_logs=False`, even for one unique reference. With streaming,
            return an owned stream of `(image_reference, bytes)` progress records,
            without global ordering across images or stdout/stderr. Empty
            iterable input returns `[]`, or a closeable empty stream when streaming.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`, or
                `timeout` is not positive and finite. Booleans are rejected
                before consuming references, even for an empty request. Inside
                an observation, `quiet=False` without streaming is rejected too.
            DockerException: A transfer or subsequent image inspection fails.
            DockerTimeout: A transfer subprocess exceeds its own deadline; it is
                killed and reaped without undoing the pull or guaranteeing that
                daemon-side work stops.
            BufferError: A streamed record or retained stderr exceeds the selected
                byte budget. Multi-image failure also cancels sibling transfers.
        """
        validate_timeout(timeout)
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )

        buffer_bytes = self.client_config.image_stream_buffer_bytes
        if buffer_bytes is None:
            buffer_bytes = self.client_config.stream_buffer_bytes

        if isinstance(x, str):
            result = self._pull_single_tag(
                image_name=x,
                quiet=quiet,
                stream_logs=stream_logs,
                platform=platform,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
            if stream_logs:
                assert result is not None
                return result
            return Image(self.client_config, x)

        images = list(OrderedDict.fromkeys(x))
        if len(images) == 0:
            return _EmptyImageStream() if stream_logs else []
        if len(images) == 1:
            image, *_ = images
            result = self._pull_single_tag(
                image_name=image,
                quiet=quiet,
                stream_logs=stream_logs,
                platform=platform,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
            if stream_logs:
                assert result is not None
                return result
            return [Image(self.client_config, image)]
        else:
            return self._pull_multiple_tags(
                image_names=images,
                quiet=quiet,
                stream_logs=stream_logs,
                platform=platform,
                timeout=timeout,
                buffer_bytes=buffer_bytes,
            )

    def _pull_multiple_tags(
        self,
        image_names: builtins.list[str],
        quiet: bool,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        buffer_bytes: int | None,
        timeout: float | None = None,
    ) -> builtins.list[Image] | ImageStream:
        if stream_logs:
            return self._pull_multiple_tags_stream(
                image_names=image_names,
                quiet=quiet,
                stream_logs=stream_logs,
                platform=platform,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
        else:

            def pull_one(image_name: str) -> None:
                self._pull_single_tag(
                    image_name,
                    quiet,
                    stream_logs,
                    platform,
                    buffer_bytes=buffer_bytes,
                    timeout=timeout,
                )

            map_in_contexts(pull_one, image_names)
            return [Image(self.client_config, image_name) for image_name in image_names]

    def _pull_multiple_tags_stream(
        self,
        image_names: builtins.list[str],
        quiet: bool,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        buffer_bytes: int | None,
        timeout: float | None = None,
    ) -> MultiImageStream:
        def start(image_name: str) -> MappedProcessStream[tuple[str, bytes]]:
            child = self._pull_single_tag(
                image_name,
                quiet,
                True,
                platform,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
            assert child is not None
            return child

        return MultiImageStream(
            image_names,
            start,
            buffer_bytes=buffer_bytes,
        )

    def _pull_single_tag(
        self,
        image_name: str,
        quiet: bool,
        stream_logs: bool = False,
        platform: str | None = None,
        *,
        buffer_bytes: int | None = None,
        timeout: float | None = None,
    ) -> MappedProcessStream[tuple[str, bytes]] | None:
        full_cmd = Command([*self.docker_cmd, "image", "pull"])

        if quiet:
            full_cmd.append("--quiet")

        if platform is not None and platform != "":
            full_cmd.append(f"--platform={platform}")

        full_cmd.append(image_name)

        if stream_logs:
            stream = stream_stdout_and_stderr(
                full_cmd, buffer_bytes=buffer_bytes, timeout=timeout
            )
            return MappedProcessStream(
                stream, ((image_name, line) for _, line in stream)
            )
        else:
            run(full_cmd, capture_stdout=quiet, capture_stderr=quiet, timeout=timeout)
            return None

    @overload
    def push(
        self,
        x: str | Iterable[str],
        quiet: bool = False,
        stream_logs: Literal[False] = False,
        *,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def push(
        self,
        x: str,
        quiet: bool = False,
        *,
        stream_logs: Literal[True],
        timeout: float | None = None,
    ) -> ImageStream: ...

    @overload
    def push(
        self,
        x: str,
        quiet: bool,
        stream_logs: Literal[True],
        *,
        timeout: float | None = None,
    ) -> ImageStream: ...

    @overload
    def push(
        self,
        x: Iterable[str],
        quiet: bool = False,
        *,
        stream_logs: Literal[True],
        timeout: float | None = None,
    ) -> ImageStream | None: ...

    @overload
    def push(
        self,
        x: Iterable[str],
        quiet: bool,
        stream_logs: Literal[True],
        *,
        timeout: float | None = None,
    ) -> ImageStream | None: ...

    @overload
    def push(
        self,
        x: str | Iterable[str],
        quiet: bool = False,
        stream_logs: bool = False,
        *,
        timeout: float | None = None,
    ) -> ImageStream | None: ...

    def push(
        self,
        x: str | Iterable[str],
        quiet: bool = False,
        stream_logs: bool = False,
        *,
        timeout: float | None = None,
    ) -> ImageStream | None:
        """Push image references to a registry after local inspection.

        Alias: `docker.push(...)`

        References are deduplicated and batch-inspected before any transfer,
        including streamed transfers. Blocking transfers start during the call
        with at most four workers. One unique streamed reference starts eagerly;
        several unique references start workers on first consumption, not on
        construction or context entry.

        Nonempty streams own their local CLIs. Exhaust them or use a context
        manager or `close()` on early exit; a bare loop break does not close them.
        Closing cancels local CLIs, not registry changes or daemon-side work. The
        selected image/general stream budget is snapshotted before consuming
        references or inspecting images and retained for queued children.

        Inside `observation()` pool tasks and stream workers each run in their
        own copy of the calling context, so one and several references behave
        alike, including the rejection of `quiet=False` without streaming.

        Args:
            x: Tag or repository reference, or an iterable deduplicated in
                first-seen order.
            quiet: Don't print anything.
            stream_logs: Return owned `(image_reference, bytes)` progress records
                instead of waiting for all transfers to complete.
            timeout: Positive finite seconds from each transfer subprocess's own
                launch, replacing the client default; `None` inherits it. The
                batch image inspection and executor queueing do not consume this
                budget, and each concurrent transfer gets its own deadline.

        Returns:
            An owned stream of `(image_reference, bytes)` progress records with
            no global ordering across images or stdout/stderr when streaming.
            Otherwise `None` after transfers complete. Empty iterable input
            returns `None` in both modes, without starting a transfer CLI.

        Raises:
            ValueError: `quiet=True` is combined with `stream_logs=True`, or
                `timeout` is not positive and finite. Booleans are rejected
                before consuming references, even for an empty request. An
                observation is active and `quiet=False` without streaming.
            NoSuchImage: A locally inspected reference does not exist.
            DockerException: Inspection or a push command fails.
            DockerTimeout: A transfer subprocess exceeds its own deadline; it is
                killed and reaped without undoing the push or guaranteeing that
                registry or daemon-side work stops.
            BufferError: A streamed record or retained stderr exceeds the selected
                byte budget. Multi-image failure also cancels sibling transfers.
        """
        validate_timeout(timeout)
        if quiet and stream_logs:
            raise ValueError(
                "It's not possible to have stream_logs=True and quiet=True at the same time. "
                "Only one can be activated at a time."
            )

        buffer_bytes = self.client_config.image_stream_buffer_bytes
        if buffer_bytes is None:
            buffer_bytes = self.client_config.stream_buffer_bytes

        images = list(OrderedDict.fromkeys(to_list(x)))

        # this is just to raise a correct exception if the images don't exist
        self.inspect(images)

        if len(images) == 0:
            return None
        elif len(images) == 1:
            image, *_ = images
            return self._push_single_tag(
                tag_or_repo=image,
                quiet=quiet,
                stream_logs=stream_logs,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
        else:
            return self._push_multiple_tags(
                tags_or_repos=images,
                quiet=quiet,
                stream_logs=stream_logs,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )

    def _push_multiple_tags(
        self,
        tags_or_repos: builtins.list[str],
        quiet: bool,
        stream_logs: bool,
        *,
        buffer_bytes: int | None,
        timeout: float | None = None,
    ) -> ImageStream | None:
        if stream_logs:
            return self._push_multiple_tags_stream(
                tags_or_repos=tags_or_repos,
                quiet=quiet,
                stream_logs=stream_logs,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
        else:

            def push_one(tag_or_repo: str) -> None:
                self._push_single_tag(tag_or_repo, quiet, stream_logs, timeout=timeout)

            map_in_contexts(push_one, tags_or_repos)
            return None

    def _push_multiple_tags_stream(
        self,
        tags_or_repos: builtins.list[str],
        quiet: bool,
        stream_logs: bool,
        *,
        buffer_bytes: int | None,
        timeout: float | None = None,
    ) -> MultiImageStream:
        def start(tag_or_repo: str) -> MappedProcessStream[tuple[str, bytes]]:
            child = self._push_single_tag(
                tag_or_repo,
                quiet,
                True,
                buffer_bytes=buffer_bytes,
                timeout=timeout,
            )
            assert child is not None
            return child

        return MultiImageStream(
            tags_or_repos,
            start,
            buffer_bytes=buffer_bytes,
        )

    def _push_single_tag(
        self,
        tag_or_repo: str,
        quiet: bool,
        stream_logs: bool,
        *,
        buffer_bytes: int | None = None,
        timeout: float | None = None,
    ) -> MappedProcessStream[tuple[str, bytes]] | None:
        full_cmd = Command([*self.docker_cmd, "image", "push"])
        full_cmd.add_flag("--quiet", quiet)
        full_cmd.append(tag_or_repo)
        if stream_logs:
            stream = stream_stdout_and_stderr(
                full_cmd, buffer_bytes=buffer_bytes, timeout=timeout
            )
            return MappedProcessStream(
                stream, ((tag_or_repo, line) for _, line in stream)
            )
        else:
            run(full_cmd, capture_stdout=quiet, capture_stderr=quiet, timeout=timeout)
            return None

    def remove(
        self,
        x: ValidImage | Iterable[ValidImage],
        force: bool = False,
        prune: bool = True,
    ) -> None:
        """Remove one or more docker images.

        Args:
            x: Single image or iterable of Docker images to remove. You can use tags or
                `pywhales.Image` objects.
            force: Force removal of the image(s).
            prune: Delete untagged parents.

        Raises:
            NoSuchImage: A selected image does not exist.
        """
        images = to_list(x)
        if not images:
            return
        full_cmd = Command([*self.docker_cmd, "image", "rm"])
        full_cmd.add_flag("--force", force)
        full_cmd.add_flag("--no-prune", not prune)
        full_cmd.extend(images)
        run(full_cmd)

    @overload
    def save(
        self,
        images: ValidImage | Iterable[ValidImage],
        output: None = None,
        *,
        timeout: float | None = None,
    ) -> Iterator[bytes]: ...

    @overload
    def save(
        self,
        images: ValidImage | Iterable[ValidImage],
        output: ValidPath,
        *,
        timeout: float | None = None,
    ) -> None: ...

    @overload
    def save(
        self,
        images: ValidImage | Iterable[ValidImage],
        output: ValidPath | None,
        *,
        timeout: float | None = None,
    ) -> Iterator[bytes] | None: ...

    def save(
        self,
        images: ValidImage | Iterable[ValidImage],
        output: ValidPath | None = None,
        *,
        timeout: float | None = None,
    ) -> Iterator[bytes] | None:
        """Save one or more images as a Docker tar archive.

        Alias: `docker.save(...)`

        Inspection occurs eagerly before returning; its CLI launch has its own
        deadline (the explicit `timeout`, otherwise the configured default) that
        is independent of the save launch's. An output path runs the save CLI
        during the call; omitted output returns a generator whose save CLI starts
        on first iteration.
        Exhaust or close that generator to leave its subprocess context. The
        selected deadline (explicit `timeout`, otherwise the configured default)
        reaps the CLI while consumption is paused and is reported on resumption.
        Archive bytes have no progress-stream buffer budget; no daemon rollback
        is guaranteed.

        A lazy archive applies the observation active at its first iteration,
        not at this call: that launch checks cancellation and the argv and
        environment limits, and keeps the scope's deadline and tokens after the
        `with` block exits. A scope that ends first kills and reaps the CLI and
        is reported on resumption.

        Args:
            images: Single image or non-empty iterable of images to save.
            output: Path of the tar archive to produce. If `output` is None, a generator
                of bytes is produced. It can be used to stream those bytes elsewhere,
                to another Docker daemon for example.
            timeout: Positive finite seconds from each local CLI launch (the
                inspection and the save), replacing the client default; `None`
                inherits it. Each launch gets its own deadline. For a lazy
                archive the save clock starts on first iteration, using the
                client default as configured at that moment when `timeout` is
                omitted.

        Returns:
            An iterator of archive-byte chunks when `output=None`; these are
            archive payload, not `(source, bytes)` progress records. Otherwise
            `None` after the local CLI completes.

        Raises:
            ValueError: No images were supplied, or `timeout` is not positive
                and finite. Booleans are rejected before inspection.
            NoSuchImage: A requested image does not exist.
            DockerException: Inspection or the save command fails, with lazy save
                failures delivered during iteration.
            DockerTimeout: The save CLI exceeds its deadline; it is killed and
                reaped without guaranteeing that daemon-side work stops.
            ObservationTimeout: The launch-time observation deadline passed first.
            ObservationCancelled: The launch-time observation token was set.
            ObservationLimitError: The observation active at first iteration
                rejects the argv or environment, before the CLI starts.
        """
        validate_timeout(timeout)
        image_list = [images] if isinstance(images, (str, Image)) else list(images)
        if len(image_list) == 0:
            raise ValueError("One or more images must be provided")

        # Trigger an exception early if an image doesn't exist. The scope ends
        # before any generator yields, so it never spans a suspension point.
        with execution_timeout(self.client_config, timeout):
            self.inspect([str(image) for image in image_list])

        full_cmd = Command([*self.docker_cmd, "image", "save"])
        full_cmd.add_simple_arg("--output", output)
        full_cmd.extend(image_list)
        if output is None:
            # we stream the bytes
            return self._save_generator(full_cmd, timeout)
        else:
            run(full_cmd, timeout=timeout)

    def _save_generator(
        self, full_cmd: Sequence[object], timeout: float | None
    ) -> Iterator[bytes]:
        """Launch an archive export on first iteration and yield stdout bytes.

        Args:
            full_cmd: Complete Docker save arguments to stringify at launch.
            timeout: Validated per-call seconds, or `None` to select the client
                default when the generator first runs.

        Yields:
            Archive-byte chunks from stdout, with no progress-record framing or
            configured payload budget. Exhaustion or generator closure closes
            pipes, reaps the local child, and joins its accounting thread before
            completing telemetry. Closure kills a still-running local child.

        Raises:
            NoSuchImage: The save CLI reports a missing image.
            DockerException: The save CLI exits unsuccessfully.
            ObservationTimeout: The observation deadline captured at first
                iteration passed before the archive was delivered.
            ObservationCancelled: A token of that observation was set.
            ObservationLimitError: That observation rejects the argv or environment.
        """
        policy, selected_timeout, command, environment, observation = (
            _prepare_archive_launch(full_cmd, timeout, "archive_save")
        )
        with ExecutionTelemetry(policy.telemetry_observer, "archive_save") as telemetry:
            telemetry.start()
            deadline = (
                None if selected_timeout is None else monotonic() + selected_timeout
            )
            with _launch_archive(
                command, environment, observation, stderr=PIPE
            ) as process:
                assert process.stdout is not None
                assert process.stderr is not None
                owner = _own_archive(
                    process,
                    command,
                    deadline,
                    raw=policy.raw_diagnostics,
                    observation=observation,
                )
                try:
                    if not owner.bounded:
                        yield from owner.lines(process.stdout)
                        exit_code = process.wait(0.1)
                    else:
                        while chunk := owner.read(process.stdout):
                            yield chunk
                        owner.check()
                        exit_code = process.wait()
                        owner.check()
                    if exit_code != 0:
                        with owner.lock:
                            os.set_blocking(process.stderr.fileno(), True)
                            stderr = process.stderr.read()
                            owner.record_read(process.stderr, stderr)
                        if "No such image" in stderr.decode():
                            raise NoSuchImage(
                                command,
                                exit_code,
                                stderr=stderr,
                                raw_diagnostics=policy.raw_diagnostics,
                            )
                        raise DockerException(
                            command,
                            exit_code,
                            stderr=stderr,
                            raw_diagnostics=policy.raw_diagnostics,
                        )
                finally:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    process.wait()
                    owner.thread.join()
                    telemetry.return_code = process.returncode
                    telemetry.stdin_bytes = owner.stdin_bytes
                    telemetry.stdout_bytes = owner.stdout_bytes
                    telemetry.stderr_bytes = owner.stderr_bytes

    def tag(self, source_image: Image | str, new_tag: str) -> None:
        """Add a tag to a Docker image.

        Alias: `docker.tag(...)`

        Args:
            source_image: The Docker image to tag. You can use a tag to reference it.
            new_tag: The tag to add to the Docker image.

        Raises:
            NoSuchImage: The source image does not exist.
        """
        full_cmd = Command(
            [
                *self.docker_cmd,
                "image",
                "tag",
                str(source_image),
                new_tag,
            ]
        )
        run(full_cmd)

    def _pull_if_necessary(self, image: ValidImage) -> Image:
        if isinstance(image, Image):
            # A docker image object must exist in the daemon to be defined.
            return image
        try:
            return self.inspect(image)
        except DockerException:
            if self.client_config.raw_diagnostics:
                print(f"Unable to find image '{image}' locally")
            return self.pull(image)

    def copy_from(
        self,
        image: ValidImage,
        path_in_image: ValidPath,
        destination: ValidPath,
        pull: str = "missing",
        *,
        timeout: float | None = None,
    ) -> None:
        """Copy files out of an image through a temporary container.

        The temporary container is removed on context exit, including copy
        failure. The source image is retained.

        Args:
            image: Source image handle or reference.
            path_in_image: Source path inside the image.
            destination: Local destination path.
            pull: Image acquisition policy forwarded to container creation;
                defaults to `"missing"`.
            timeout: Positive finite seconds from each local CLI launch this
                helper makes (inspection, pull, container create, copy, stop
                and removal), replacing the client default; `None` inherits it.
                Each launch has its own deadline rather than one shared budget.
                The configuration is not mutated.

        Raises:
            ValueError: `timeout` is not positive and finite. Booleans are
                rejected before any CLI is resolved or launched.
            DockerTimeout: A launched local CLI exceeds its deadline; it is
                killed and reaped without guaranteeing that daemon-side work
                stops. The temporary container is still removed if it was
                created.
        """
        validate_timeout(timeout)
        with (
            execution_timeout(self.client_config, timeout),
            ContainerCLI(self.client_config).create(image, pull=pull) as tmp_container,
        ):
            tmp_container.copy_from(path_in_image, destination, timeout=timeout)

    def copy_to(
        self,
        base_image: ValidImage,
        local_path: ValidPath,
        path_in_image: ValidPath,
        new_tag: str | None = None,
        pull: str = "missing",
        *,
        timeout: float | None = None,
    ) -> Image:
        """Copy local files into a temporary container and commit a new image.

        The temporary container is removed on context exit; the original and
        committed images are retained.

        Args:
            base_image: Source image handle or reference.
            local_path: Local source path.
            path_in_image: Destination path inside the new image.
            new_tag: Optional tag for the committed image; `None` omits a tag.
            pull: Image acquisition policy forwarded to container creation;
                defaults to `"missing"`.
            timeout: Positive finite seconds from each local CLI launch this
                helper makes (inspection, pull, container create, copy, commit,
                stop and removal), replacing the client default; `None`
                inherits it. Each launch has its own deadline rather than one
                shared budget. The configuration is not mutated.

        Returns:
            The committed image using the originating client configuration.

        Raises:
            ValueError: `timeout` is not positive and finite. Booleans are
                rejected before any CLI is resolved or launched.
            DockerTimeout: A launched local CLI exceeds its deadline; it is
                killed and reaped without guaranteeing that daemon-side work
                stops. The temporary container is still removed if it was
                created.
        """
        validate_timeout(timeout)
        with (
            execution_timeout(self.client_config, timeout),
            ContainerCLI(self.client_config).create(
                base_image, pull=pull
            ) as tmp_container,
        ):
            tmp_container.copy_to(local_path, path_in_image, timeout=timeout)
            return tmp_container.commit(tag=new_tag)
