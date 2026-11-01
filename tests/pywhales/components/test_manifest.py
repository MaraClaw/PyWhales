import json
import os
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Image
from pywhales.components.manifest.cli_wrapper import (
    ManifestList,
    ManifestListInspectResult,
)
from pywhales.test_utils import get_all_jsons, random_name


@pytest.fixture
def manifest(docker_client: DockerClient) -> Generator[ManifestList]:
    """Pull fixed BusyBox tags and yield a temporary manifest list.

    Requires a disposable Docker daemon and registry access. The manifest context
    owns its removal; normal teardown also removes both pulled tags, even if they
    existed before the fixture.

    Yields:
        The manifest handle while its context is active.
    """
    manifest_name = random_name()
    # utilizing old, pre-manifest-list images
    images = ["busybox:1.26", "busybox:1.27.1"]
    docker_client.image.pull(images)
    with docker_client.manifest.create(manifest_name, images) as my_manifest:
        yield my_manifest
    docker_client.image.remove(images)


@pytest.fixture
def platform_variant_manifest(
    docker_client: DockerClient, request: pytest.FixtureRequest
) -> Image:
    """Pull and inspect an ARMv7 image with removal registered before acquisition.

    Requires Docker and registry access. The finalizer removes the fixed image
    tag, including a pre-existing tag, so use a disposable daemon.

    Returns:
        The inspected platform-specific image handle, valid until teardown.
    """
    image_with_platform_variant = "arm32v7/busybox:1.35"

    def remove_docker_image() -> None:
        docker_client.image.remove(image_with_platform_variant)

    request.addfinalizer(remove_docker_image)
    docker_client.image.pull(
        image_with_platform_variant, quiet=True, platform="linux/arm/v7"
    )
    return docker_client.image.inspect(image_with_platform_variant)


@pytest.mark.parametrize("json_file", get_all_jsons("manifests"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    ManifestListInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


@pytest.mark.skipif(
    os.environ.get("CI", "false") == "true",
    reason="The creation of manifest doesn't work in github actions for some reason",
)
def test_manifest_create_remove(
    docker_client: DockerClient, manifest: ManifestList
) -> None:
    assert isinstance(manifest, ManifestList)


@pytest.mark.skipif(
    os.environ.get("CI", "false") == "true",
    reason="The creation of manifest doesn't work in github actions for some reason",
)
def test_manifest_annotate(docker_client: DockerClient, manifest: ManifestList) -> None:
    with docker_client.manifest.create(random_name(), ["busybox:1.26"]) as target:
        assert target.manifests is not None
        assert len(target.manifests) == 1
        digest = target.manifests[0].digest
        assert digest is not None

    assert manifest.name is not None
    docker_client.manifest.annotate(
        manifest.name, "busybox:1.26", os="linux", arch="arm64"
    )
    manifest.reload()
    assert manifest.manifests is not None
    matching = [entry for entry in manifest.manifests if entry.digest == digest]
    assert len(matching) == 1
    platform = matching[0].platform
    assert platform is not None
    assert platform.os == "linux"
    assert platform.architecture == "arm64"


def test_manifest_platform_variant(platform_variant_manifest: Image) -> None:
    assert "linux" in repr(platform_variant_manifest.os)
    assert "arm" in repr(platform_variant_manifest.architecture)
    assert "v7" in repr(platform_variant_manifest.variant)


@pytest.mark.parametrize("insecure", [False, True])
def test_manifest_inspect_insecure_flag(
    docker_client: DockerClient, insecure: bool
) -> None:
    ref = "busybox:latest"

    fake_json = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "manifests": [],
    }

    with patch("pywhales.components.manifest.cli_wrapper.run") as mock_run:
        mock_run.return_value = json.dumps(fake_json)

        manifest: ManifestList = docker_client.manifest.inspect(ref, insecure=insecure)

        assert manifest.insecure == insecure

        cmd = list(mock_run.call_args[0][0])

        if insecure:
            assert "--insecure" in cmd
        else:
            assert "--insecure" not in cmd


@pytest.mark.parametrize("timeout", [None, 2.5])
def test_manifest_push_forwards_timeout_and_keeps_preflight_unbudgeted(
    docker_client: DockerClient, timeout: float | None
) -> None:
    with patch("pywhales.components.manifest.cli_wrapper.run") as mock_run:
        mock_run.return_value = "{}"

        docker_client.manifest.push("busybox:latest", timeout=timeout)

    inspect_call, push_call = mock_run.call_args_list
    assert "timeout" not in inspect_call.kwargs
    assert push_call.kwargs["timeout"] == timeout


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), True])
def test_manifest_push_invalid_timeout_precedes_preflight(
    docker_client: DockerClient, timeout: float
) -> None:
    with (
        patch("pywhales.components.manifest.cli_wrapper.run") as mock_run,
        pytest.raises(ValueError),
    ):
        docker_client.manifest.push("busybox:latest", timeout=timeout)

    mock_run.assert_not_called()


EMPTY_SELECTIONS = [
    pytest.param(list, id="list"),
    pytest.param(tuple, id="tuple"),
    pytest.param(lambda: (item for item in ()), id="generator"),
]
SINGLE_SELECTIONS = [
    pytest.param(lambda: "alpha", id="str"),
    pytest.param(lambda: ["alpha"], id="list"),
    pytest.param(lambda: (item for item in ["alpha"]), id="generator"),
]


@pytest.mark.parametrize("make_empty", EMPTY_SELECTIONS)
def test_manifest_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.manifest.remove(make_empty())


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_manifest_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.manifest.remove(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "manifest", "rm", "alpha"]
