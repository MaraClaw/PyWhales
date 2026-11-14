"""Static contracts for image history, search, tree listing and platform options.

The ty check validates inferred shapes. Pytest does not collect or execute the
_check_* functions or their Docker calls.
"""

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import assert_type

from pywhales import DockerClient, Image
from pywhales.components.image.cli_wrapper import ImageSearchFilter
from pywhales.components.image.models import (
    ImageHistoryLayer,
    ImageInspectResult,
    ImageSearchResult,
)
from pywhales.components.image.streams import EmptyImageStream, ImageStream


def _check_history_and_search(
    client: DockerClient,
    image: Image,
    filters: Iterable[ImageSearchFilter],
    timeout: float | None,
) -> None:
    assert_type(client.image.history("alpine"), list[ImageHistoryLayer])
    assert_type(
        client.image.history(image, platform="linux/amd64"), list[ImageHistoryLayer]
    )
    assert_type(
        client.image.history("alpine", platform=None, timeout=timeout),
        list[ImageHistoryLayer],
    )
    assert_type(image.history(), list[ImageHistoryLayer])
    assert_type(
        image.history(platform="linux/amd64", timeout=timeout), list[ImageHistoryLayer]
    )
    assert_type(client.image.search("alpine"), list[ImageSearchResult])
    assert_type(
        client.image.search("alpine", filters=filters, limit=3, timeout=timeout),
        list[ImageSearchResult],
    )
    assert_type(
        client.image.search("alpine", filters={"stars": "10"}, limit=None),
        list[ImageSearchResult],
    )
    assert_type(
        client.image.search("alpine", filters=[("is-official", "true")]),
        list[ImageSearchResult],
    )
    layer = client.image.history("alpine")[0]
    assert_type(layer.id, str | None)
    assert_type(layer.size, int)
    assert_type(client.image.search("alpine")[0].is_official, bool)


def _check_tree_listing(client: DockerClient, flag: bool) -> None:
    assert_type(client.image.list(), list[Image])
    assert_type(client.image.list("alpine", [("reference", "alp*")], True), list[Image])
    assert_type(client.image.list(hydrate=True), list[Image])
    assert_type(client.image.list(tree=False), list[Image])
    assert_type(client.image.list(tree=True), str)
    assert_type(client.image.list("alpine", all=True, tree=True), str)
    assert_type(client.image.list(tree=flag), list[Image] | str)
    assert_type(client.images(tree=True), str)
    assert_type(client.images(), list[Image])


def _check_inspect_platform(
    client: DockerClient, references: Iterable[str], platform: str | None
) -> None:
    assert_type(client.image.inspect("alpine"), Image)
    assert_type(client.image.inspect(references), list[Image])
    assert_type(client.image.inspect("alpine", platform=None), Image)
    assert_type(
        client.image.inspect("alpine", platform="linux/arm64"), ImageInspectResult
    )
    assert_type(
        client.image.inspect(references, platform="linux/arm64"),
        list[ImageInspectResult],
    )
    assert_type(
        client.image.inspect("alpine", platform=platform), Image | ImageInspectResult
    )
    assert_type(
        client.image.inspect(references, platform=platform),
        list[Image] | list[ImageInspectResult],
    )


def _check_transfer_options(
    client: DockerClient,
    references: Iterable[str],
    flag: bool,
    timeout: float | None,
) -> None:
    assert_type(client.image.pull("alpine"), Image)
    assert_type(client.image.pull(references), list[Image])
    assert_type(client.image.pull("repo", all_tags=True), None)
    assert_type(
        client.image.pull("repo", True, platform="linux/arm64", all_tags=True), None
    )
    assert_type(
        client.image.pull("repo", stream_logs=True, all_tags=True, timeout=timeout),
        ImageStream,
    )
    assert_type(client.pull("repo", all_tags=True), None)
    assert_type(
        client.image.pull("repo", all_tags=flag),
        Image | list[Image] | ImageStream | EmptyImageStream | None,
    )
    assert_type(client.image.push("alpine"), None)
    assert_type(client.image.push("alpine", platform="linux/arm64"), None)
    assert_type(client.image.push(references, platform="linux/arm64"), None)
    assert_type(
        client.image.push("alpine", stream_logs=True, platform="linux/arm64"),
        ImageStream,
    )
    assert_type(client.image.push("repo", all_tags=True), None)
    assert_type(
        client.image.push(
            "repo", stream_logs=True, all_tags=True, platform="linux/arm64"
        ),
        ImageStream,
    )
    assert_type(client.image.push("repo", all_tags=flag), ImageStream | None)
    assert_type(client.push("repo", all_tags=True), None)


def _check_archive_platforms(
    client: DockerClient, image: Image, output: Path | None, platforms: Iterable[str]
) -> None:
    assert_type(client.image.load(b"archive", platform="linux/amd64"), list[str])
    assert_type(client.image.load(Path("a.tar"), platform=platforms), list[str])
    assert_type(client.image.load(iter([b"archive"]), platform=None), list[str])
    assert_type(client.image.save("alpine", platform="linux/amd64"), Iterator[bytes])
    assert_type(client.image.save("alpine", Path("a.tar"), platform=platforms), None)
    assert_type(
        client.image.save("alpine", output, platform=platforms), Iterator[bytes] | None
    )
    assert_type(image.save(platform="linux/amd64"), Iterator[bytes])
    assert_type(image.save(Path("a.tar"), platform=platforms), None)
    assert_type(client.image.remove("alpine", platform="linux/amd64"), None)
    assert_type(client.image.remove(image, True, False, platform=platforms), None)
    assert_type(image.remove(platform="linux/amd64"), None)
