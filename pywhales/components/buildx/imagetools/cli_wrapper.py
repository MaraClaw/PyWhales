"""Buildx registry-manifest operations that do not pull local images."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, overload

from pywhales._json import loads_compat
from pywhales.client_config import Command, DockerCLICaller
from pywhales.components._json_output import JsonOutputParser
from pywhales.utils import ValidPath, run

from .models import Manifest

type ImagetoolsProgress = Literal["auto", "none", "plain", "rawjson", "tty"]


def _parse_manifest(output: str) -> Manifest:
    return Manifest(**loads_compat(output))


class ImagetoolsCLI(DockerCLICaller):
    """Registry manifest commands using the configured Docker Buildx plugin."""

    @overload
    def inspect(
        self,
        name: str,
        *,
        builder: str | None = None,
        debug: Literal[True],
    ) -> str: ...

    @overload
    def inspect(
        self,
        name: str,
        *,
        builder: str | None = None,
        debug: Literal[False] = False,
    ) -> Manifest: ...

    @overload
    def inspect(
        self,
        name: str,
        *,
        builder: str | None = None,
        debug: bool = False,
    ) -> Manifest | str: ...

    def inspect(
        self,
        name: str,
        *,
        builder: str | None = None,
        debug: bool = False,
    ) -> Manifest | str:
        """Return a registry manifest or index without pulling the image.

        Args:
            name: Registry image reference accepted by Buildx imagetools.
            builder: Builder name; `None` leaves selection to Buildx.
            debug: Pass `--debug`. The call then drops `--raw` and returns the
                human-readable text, a newline and the captured debug log
                written to stderr.

        Returns:
            The parsed raw registry manifest, not a local image handle, or the
            combined text when `debug=True`.

        Raises:
            ParsingError: If the output is malformed or has an unexpected shape.
                Safe mode withholds the payload and chains no cause; a client
                with `raw_diagnostics=True` chains the original error.
        """
        full_cmd = Command([*self.docker_cmd, "buildx", "imagetools", "inspect"])
        full_cmd.add_flag("--debug", debug)
        full_cmd.add_simple_arg("--builder", builder)
        if debug:
            full_cmd.append(name)
            stdout, stderr = run(full_cmd, return_stderr=True)
            return f"{stdout}\n{stderr}"
        full_cmd.extend(["--raw", name])
        result = run(full_cmd)
        parser = JsonOutputParser(
            "imagetools manifest", self.client_config.raw_diagnostics
        )
        return parser.parse(_parse_manifest, result)

    @overload
    def create(
        self,
        sources: Sequence[str] = (),
        tags: Sequence[str] = (),
        append: bool = False,
        annotations: Mapping[str, str] = MappingProxyType({}),
        files: Sequence[str | Path] = (),
        *,
        dry_run: Literal[True],
        builder: str | None = None,
        prefer_index: bool | None = None,
        metadata_file: ValidPath | None = None,
        platforms: Sequence[str] = (),
        progress: ImagetoolsProgress | None = None,
        debug: bool = False,
    ) -> Manifest: ...

    @overload
    def create(
        self,
        sources: Sequence[str] = (),
        tags: Sequence[str] = (),
        append: bool = False,
        annotations: Mapping[str, str] = MappingProxyType({}),
        files: Sequence[str | Path] = (),
        dry_run: Literal[False] = False,
        builder: str | None = None,
        *,
        prefer_index: bool | None = None,
        metadata_file: ValidPath | None = None,
        platforms: Sequence[str] = (),
        progress: ImagetoolsProgress | None = None,
        debug: bool = False,
    ) -> None: ...

    @overload
    def create(
        self,
        sources: Sequence[str] = (),
        tags: Sequence[str] = (),
        append: bool = False,
        annotations: Mapping[str, str] = MappingProxyType({}),
        files: Sequence[str | Path] = (),
        dry_run: bool = False,
        builder: str | None = None,
        *,
        prefer_index: bool | None = None,
        metadata_file: ValidPath | None = None,
        platforms: Sequence[str] = (),
        progress: ImagetoolsProgress | None = None,
        debug: bool = False,
    ) -> Manifest | None: ...

    def create(
        self,
        sources: Sequence[str] = (),
        tags: Sequence[str] = (),
        append: bool = False,
        annotations: Mapping[str, str] = MappingProxyType({}),
        files: Sequence[str | Path] = (),
        dry_run: bool = False,
        builder: str | None = None,
        *,
        prefer_index: bool | None = None,
        metadata_file: ValidPath | None = None,
        platforms: Sequence[str] = (),
        progress: ImagetoolsProgress | None = None,
        debug: bool = False,
    ) -> Manifest | None:
        """Create a registry manifest from existing source manifests.

        Sources must already exist in the destination registry. The call waits
        for Buildx to finish; normal creation publishes to the registry, whereas
        dry-run mode returns the proposed manifest without publishing it.

        Args:
            sources: Sequence of source manifest or index references. A bare
                string is rejected; the default passes no source references.
            tags: Sequence of destination tags; a bare string is rejected.
            append: Append sources to an existing manifest.
            annotations: Mapping of annotation names to values.
            files: Sequence of source-descriptor paths; a bare string is rejected.
            dry_run: Return the proposed manifest instead of publishing it.
            builder: Builder name; `None` leaves selection to Buildx.
            prefer_index: With a single source, `True` outputs an index or
                manifest list and `False` makes a carbon copy; `None` leaves
                the Buildx default, which prefers an index.
            metadata_file: Path where Buildx writes the create result metadata;
                `None` omits the option.
            platforms: Sequence of platforms to keep in the target image, passed
                as repeated `--platform` flags; a bare string is rejected.
            progress: Progress rendering mode; `None` leaves the Buildx default,
                `auto`.
            debug: Pass `--debug`; the log is written to stderr, which this call
                does not return.

        Returns:
            The parsed proposed manifest when `dry_run=True`; otherwise `None`
            after the publishing command completes.

        Raises:
            TypeError: `sources`, `tags`, `files`, or `platforms` is not a
                non-string sequence, or `annotations` is not a mapping.
            DockerException: The Buildx command fails.
        """
        if not isinstance(sources, Sequence) or isinstance(sources, str):
            raise TypeError(
                "The argument 'sources' of the function docker.buildx.imagetools.create() must be a list of strings."
            )
        if not isinstance(tags, Sequence) or isinstance(tags, str):
            raise TypeError(
                "The argument 'tags' of the function docker.buildx.imagetools.create() must be a list of strings."
            )
        if not isinstance(files, Sequence) or isinstance(files, str):
            raise TypeError(
                "The argument 'files' of the function docker.buildx.imagetools.create() must be a list of strings."
            )
        if not isinstance(platforms, Sequence) or isinstance(platforms, str):
            raise TypeError(
                "The argument 'platforms' of the function docker.buildx.imagetools.create() must be a list of strings."
            )
        if not isinstance(annotations, Mapping):
            raise TypeError(
                "The argument 'annotations' of the function docker.buildx.imagetools.create() must be a dict."
            )

        full_cmd = Command([*self.docker_cmd, "buildx", "imagetools", "create"])
        full_cmd.add_flag("--debug", debug)
        for tag in tags:
            full_cmd.add_simple_arg("--tag", tag)
        for key, value in annotations.items():
            full_cmd.add_simple_arg("--annotation", f"{key}={value}")
        for file in files:
            full_cmd.add_simple_arg("--file", file)
        full_cmd.add_simple_arg("--builder", builder)
        full_cmd.add_flag("--append", append)
        full_cmd.add_flag("--dry-run", dry_run)
        if prefer_index is not None:
            full_cmd.append(
                "--prefer-index" if prefer_index else "--prefer-index=false"
            )
        full_cmd.add_simple_arg("--metadata-file", metadata_file)
        full_cmd.add_args_iterable("--platform", platforms)
        full_cmd.add_simple_arg("--progress", progress)
        full_cmd += sources

        result = run(full_cmd)
        if dry_run:
            parser = JsonOutputParser(
                "imagetools manifest", self.client_config.raw_diagnostics
            )
            return parser.parse(_parse_manifest, result)
