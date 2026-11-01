"""Buildx regressions with global-client integration and mocked transport cases.

Live cases require Docker, a disposable daemon, and Buildx. Fixtures can switch
the selected builder, prune shared build cache, and create or remove images.
Global-client calls are not redirected by the docker_client fixture option.
"""

import json
import os
import sys
import tarfile
from collections.abc import Generator, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, assert_never
from unittest.mock import Mock, patch

import pytest

import pywhales.components.buildx.cli_wrapper
from pywhales import DockerClient, docker
from pywhales.exceptions import DockerException, NoSuchImage
from pywhales.test_utils import set_cache_validity_period
from pywhales.utils import PROJECT_ROOT, stream_stdout_and_stderr

dockerfile_content1 = """
FROM busybox
RUN touch /dada
"""

dockerfile_content2 = """
FROM busybox
COPY --from=test_context README.md /README.md
"""

dockerfile_content3 = """
FROM test_context
RUN touch /dada
"""

dockerfile_content4 = """
FROM busybox
COPY --from=test_context /dada /dada
"""


@pytest.fixture
def with_docker_driver() -> Generator[None]:
    """Select the default builder and restore the previous builder at teardown.

    Requires Docker and Buildx; the selected-builder state is shared with other
    CLI operations using this configuration.

    Yields:
        Control while the default builder is selected.
    """
    current_builder = docker.buildx.inspect()
    docker.buildx.use("default")
    yield
    docker.buildx.use(current_builder)


@pytest.fixture
def with_container_driver() -> Generator[None]:
    """Select a temporary builder and remove it before restoring the selection.

    Requires Docker and Buildx. Restoration follows normal fixture resumption;
    builder context exit owns removal.

    Yields:
        Control while the newly created builder is selected.
    """
    current_builder = docker.buildx.inspect()
    with docker.buildx.create(use=True):
        yield
    docker.buildx.use(current_builder)


@pytest.fixture
def prune_all() -> None:
    """Prune all build cache on the selected builder of the disposable daemon."""
    docker.buildx.prune(all=True)


@pytest.fixture
def with_oci_layout_compliant_dir(tmp_path: Path) -> None:
    """Build and extract an OCI layout under the pytest temporary directory.

    Requires Docker and Buildx. Use a temporary selected builder for the build,
    remove it on context exit, and restore the prior selection before extraction.
    """
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)

    # Build the oci layout compliant directory
    tar_path = os.path.join(tmp_path, "oci-layout.tar")
    oci_folder_path = os.path.join(tmp_path, "oci-layout")

    current_builder = docker.buildx.inspect()
    with docker.buildx.create(use=True):
        docker.buildx.build(tmp_path, output={"type": "oci", "dest": tar_path})
    docker.buildx.use(current_builder)

    # Extract tar to directory
    with tarfile.open(tar_path) as tar:
        tar.extractall(oci_folder_path)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    my_image = docker.buildx.build(tmp_path)
    assert my_image is not None
    assert my_image.size is not None
    assert my_image.size > 1000


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_streaming_logs(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    output = list(docker.buildx.build(tmp_path, stream_logs=True))
    assert len(output) > 1
    for line in output:
        assert line != ""


@patch("pywhales.components.buildx.cli_wrapper.stream_stdout_and_stderr")
@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_secrets(ssas: Mock, tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    list(docker.buildx.build(tmp_path, stream_logs=True))
    assert "--secret" not in ssas.call_args[0][0]
    list(docker.buildx.build(tmp_path, secrets="secret1", stream_logs=True))
    assert "--secret" in ssas.call_args[0][0] and "secret1" in ssas.call_args[0][0]
    list(
        docker.buildx.build(tmp_path, secrets=["secret1", "secret2"], stream_logs=True)
    )
    assert (
        "--secret" in ssas.call_args[0][0]
        and "secret1" in ssas.call_args[0][0]
        and "secret2" in ssas.call_args[0][0]
    )


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_streaming_logs_with_decode_error_handling(tmp_path: Path) -> None:
    # This will simulate buildx clipping log output in the middle of a UTF-8 character
    bad_encoding_dockerfile = (
        dockerfile_content1
        + """
    RUN printf '\\xE2\\x9E'
    """
    )
    (tmp_path / "Dockerfile").write_text(bad_encoding_dockerfile)
    output = list(docker.buildx.build(tmp_path, cache=False, stream_logs=True))
    assert len([x for x in output if "�" in x]) == 1
    assert "#7 DONE" in output[-1]


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_load_docker_driver(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    my_image = docker.buildx.build(tmp_path, load=True)
    assert my_image is not None
    assert my_image.size is not None
    assert my_image.size > 1000


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_push_registry(tmp_path: Path, docker_registry: str) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    metadata_path = tmp_path / "metadata.json"
    with docker.buildx.create(use=True, driver_options={"network": "host"}):
        result = docker.buildx.build(
            tmp_path,
            push=True,
            tags=f"{docker_registry}/dodo",
            metadata_file=metadata_path,
        )
    assert result is None
    docker.pull(f"{docker_registry}/dodo")
    assert metadata_path.is_file()
    metadata_content = metadata_path.read_text().strip()
    assert metadata_content
    json.loads(metadata_content)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_push_registry_override_builder(
    tmp_path: Path, docker_registry: str
) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    with docker.buildx.create(driver_options={"network": "host"}) as my_builder:
        output = docker.buildx.build(
            tmp_path, push=True, tags=f"{docker_registry}/dodo", builder=my_builder
        )
    assert output is None
    docker.pull(f"{docker_registry}/dodo")


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_caching(tmp_path: Path, docker_registry: str) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    cache = f"{docker_registry}/project:cache"
    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(tmp_path, cache_to=cache)

    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(tmp_path, cache_from=cache)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_caching_both_name_time(tmp_path: Path, docker_registry: str) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    cache = f"{docker_registry}/project:cache"
    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(tmp_path, cache_to=cache, cache_from=cache)

    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(tmp_path, cache_to=cache, cache_from=cache)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_caching_dict_form(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(
            tmp_path,
            cache_to={"type": "local", "dest": tmp_path / "cache", "mode": "max"},
        )

    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(
            tmp_path, cache_from={"type": "local", "src": tmp_path / "cache"}
        )


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_caching_list_form(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(
            tmp_path,
            cache_from=[
                {"type": "local", "src": tmp_path / "cache"},
                {"type": "local", "src": tmp_path / "cache2"},
            ],
        )


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_output_type_registry(
    tmp_path: Path, docker_registry: str
) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    with docker.buildx.create(use=True, driver_options={"network": "host"}):
        output = docker.buildx.build(
            tmp_path, output={"type": "registry"}, tags=f"{docker_registry}/dodo"
        )
    assert output is None
    docker.pull(f"{docker_registry}/dodo")


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_push_registry_multiple_tags(
    tmp_path: Path, docker_registry: str
) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    tags = [f"{docker_registry}/dodo:1", f"{docker_registry}/dada:1"]
    with docker.buildx.create(use=True, driver_options={"network": "host"}):
        output = docker.buildx.build(tmp_path, push=True, tags=tags)
    assert output is None
    docker.pull(f"{docker_registry}/dodo:1")
    docker.pull(f"{docker_registry}/dada:1")


@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_load_container_driver(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    my_image = docker.buildx.build(tmp_path, load=True)

    # can only be fixed by fixing https://github.com/docker/buildx/issues/420
    assert my_image is None


@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_load_container_driver_tag(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    built_image = docker.buildx.build(tmp_path, load=True, tags="my_tag:1")
    assert built_image is not None
    with built_image as my_image:
        assert my_image.repo_tags == ["my_tag:1"]


@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_load_container_driver_tags(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    built_image = docker.buildx.build(
        tmp_path, load=True, tags=["my_tag:1", "other_tag:2"]
    )
    assert built_image is not None
    with built_image as my_image:
        assert my_image.repo_tags is not None
        assert set(my_image.repo_tags) == {"my_tag:1", "other_tag:2"}


@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_container_driver(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    my_image = docker.buildx.build(tmp_path)
    assert my_image is None
    # by default, loading isn't activated with the container driver.


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_output_local_docker_driver(tmp_path: Path) -> None:
    _test_buildx_build_output_local(tmp_path)


@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_output_local_container_driver(tmp_path: Path) -> None:
    _test_buildx_build_output_local(tmp_path)


def _test_buildx_build_output_local(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    my_image = docker.buildx.build(
        tmp_path, output={"type": "local", "dest": tmp_path / "my_image"}
    )
    assert my_image is None
    assert (tmp_path / "my_image/dada").is_file()


def test_multiarch_build(tmp_path: Path, docker_registry: str) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    tags = [f"{docker_registry}/dodo:1"]
    with docker.buildx.create(use=True, driver_options={"network": "host"}):
        output = docker.buildx.build(
            tmp_path, push=True, tags=tags, platforms=["linux/amd64", "linux/arm64"]
        )
    assert output is None
    docker.pull(f"{docker_registry}/dodo:1")


@pytest.mark.usefixtures("with_container_driver")
@pytest.mark.parametrize(
    "kwargs",
    [
        {"sbom": True},
        {"provenance": True},
        {"attest": {"type": "provenance", "mode": "min"}},
        {"provenance": {"mode": "max"}},
    ],
)
def test_buildx_build_attestations(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    docker.buildx.build(tmp_path, **kwargs)


# Does the build work when passing extra contexts
# without making use of them in the Dockerfile
@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_build_context1(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    docker.buildx.build(tmp_path, build_contexts={"test_context": "."})


# Does the build work when passing extra contexts
# when the Dockerfile does make use of them
@pytest.mark.usefixtures("with_container_driver")
@pytest.mark.parametrize(
    "test_context",
    [
        # Test with local directory
        PROJECT_ROOT,
        # Test with git repo
        "https://github.com/gabrieldemarmiesse/python-on-whales.git",
    ],
)
def test_buildx_build_build_context2(tmp_path: Path, test_context: str | Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content2)
    docker.buildx.build(tmp_path, build_contexts={"test_context": test_context})


# Test with oci layout compliant directory
@pytest.mark.usefixtures("with_oci_layout_compliant_dir")
@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_build_context_oci(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content4)
    docker.buildx.build(
        tmp_path,
        build_contexts={
            "test_context": f"oci-layout://{os.path.join(tmp_path, 'oci-layout')}"
        },
    )


# Test with docker image
@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_build_context_image(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content3)
    docker.buildx.build(
        tmp_path,
        build_contexts={"test_context": "docker-image://busybox:1.36.0"},
    )


# Does the build fail when NOT passing extra contexts
# when the dockerfile does make use of them
@pytest.mark.usefixtures("with_container_driver")
def test_buildx_build_build_context_fail(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content2)
    with pytest.raises(DockerException):
        docker.buildx.build(tmp_path)


def test_buildx_build_context_manager2(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    buildx_builder = docker.buildx.create()
    docker.buildx.use(buildx_builder)
    with buildx_builder:
        docker.buildx.build(tmp_path)

    # check that the builder doesn't exist
    with pytest.raises(DockerException):
        docker.buildx.inspect(buildx_builder.name)


def test_inspect() -> None:
    my_builder = docker.buildx.create(use=True)
    with my_builder:
        assert docker.buildx.inspect(my_builder.name) == my_builder
        assert docker.buildx.inspect() == my_builder


def test_buildx_inspect_bootstrap() -> None:
    my_builder = docker.buildx.create()
    with my_builder:
        docker.buildx.inspect(my_builder.name, bootstrap=True)
        assert my_builder.nodes is not None
        assert my_builder.nodes[0].status == "running"
        # Must contain at least the host native platform
        assert my_builder.nodes[0].platforms is not None
        assert len(my_builder.nodes[0].platforms) > 0


def test_builder_name() -> None:
    my_builder = docker.buildx.create(name="some_builder")
    with my_builder:
        assert my_builder.name == "some_builder"


def test_builder_options() -> None:
    my_builder = docker.buildx.create(driver_options={"network": "host"})
    my_builder.remove()


def test_builder_set_driver() -> None:
    my_builder = docker.buildx.create(driver="docker-container")
    my_builder.remove()


def test_use_builder() -> None:
    my_builder = docker.buildx.create()
    with my_builder:
        docker.buildx.use(my_builder)
        docker.buildx.use("default")
        docker.buildx.use(my_builder, default=True, global_=True)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_single_tag(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    image = docker.buildx.build(tmp_path, tags="hello1")
    try:
        assert image is not None
        assert image.repo_tags is not None
        assert "hello1:latest" in image.repo_tags
    finally:
        docker.image.remove("hello1", force=True, prune=False)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_multiple_tags(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    image = docker.buildx.build(tmp_path, tags=["hello1", "hello2"])
    try:
        assert image is not None
        assert image.repo_tags is not None
        assert "hello1:latest" in image.repo_tags
        assert "hello2:latest" in image.repo_tags
    finally:
        docker.image.remove(["hello1", "hello2"], force=True, prune=False)


@pytest.mark.usefixtures("with_docker_driver")
def test_cache_invalidity(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    with set_cache_validity_period(100):
        image = docker.buildx.build(tmp_path, tags=["hello1", "hello2"])
        try:
            assert image is not None
            docker.image.remove("hello1")
            docker.pull("hello-world")
            docker.tag("hello-world", "hello1")
            image.reload()
            assert image.repo_tags is not None
            assert "hello1:latest" not in image.repo_tags
            assert "hello2:latest" in image.repo_tags
        finally:
            docker.image.remove(["hello1", "hello2"], force=True, prune=False)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_aliases(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    docker.build(tmp_path)
    docker.image.build(tmp_path)


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_error(tmp_path: Path) -> None:
    with pytest.raises(DockerException) as e:
        docker.buildx.build(tmp_path)

    assert e.value.docker_command[1:3] == ["buildx", "build"]
    assert str(tmp_path) in e.value.docker_command


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_network(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)
    docker.buildx.build(tmp_path, network="host")


@pytest.mark.usefixtures("with_docker_driver")
def test_buildx_build_file(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile111").write_text(dockerfile_content1)
    docker.buildx.build(tmp_path, file=(tmp_path / "Dockerfile111"))


def test_buildx_create_remove() -> None:
    builder = docker.buildx.create()

    docker.buildx.remove(builder)


def test_buildx_create_bootstrap() -> None:
    my_builder = docker.buildx.create(bootstrap=True)
    with my_builder:
        assert my_builder.nodes is not None
        assert my_builder.nodes[0].status == "running"
        # Must contain at least the host native platform
        assert my_builder.nodes[0].platforms is not None
        assert len(my_builder.nodes[0].platforms) > 0


def test_buildx_create_remove_with_platforms() -> None:
    builder = docker.buildx.create(platforms=["linux/amd64", "linux/arm64"])

    assert builder.nodes is not None
    assert builder.nodes[0].platforms == ["linux/amd64", "linux/arm64"]

    docker.buildx.remove(builder)


bake_test_dir = PROJECT_ROOT / "tests/pywhales/components/bake_tests"
bake_file = bake_test_dir / "docker-bake.hcl"


@pytest.fixture
def change_cwd() -> Generator[None]:
    """Enter the bake fixture directory and restore cwd during normal teardown.

    Yields:
        Control with the process-wide current directory set to the bake fixtures.
    """
    old_cwd = os.getcwd()
    os.chdir(bake_test_dir)
    yield
    os.chdir(old_cwd)


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake(only_print: bool) -> None:
    config = docker.buildx.bake(files=[bake_file], print=only_print)
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image1:1.0.0"],
                "target": "out1",
            },
            "my_out2": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:1.0.0"],
                "target": "out2",
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_with_load(only_print: bool) -> None:
    config = docker.buildx.bake(files=[bake_file], load=True, print=only_print)
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image1:1.0.0"],
                "target": "out1",
                "output": [{"type": "docker"}],
            },
            "my_out2": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:1.0.0"],
                "target": "out2",
                "output": [{"type": "docker"}],
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_with_variables(only_print: bool) -> None:
    config = docker.buildx.bake(
        files=[bake_file], print=only_print, variables={"TAG": "3.0.4"}
    )
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image1:3.0.4"],
                "target": "out1",
            },
            "my_out2": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:3.0.4"],
                "target": "out2",
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_with_variables_2(
    only_print: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_NAME_1", "dodo")
    config = docker.buildx.bake(
        files=[bake_file], print=only_print, variables={"TAG": "3.0.4"}
    )
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["dodo:3.0.4"],
                "target": "out1",
            },
            "my_out2": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:3.0.4"],
                "target": "out2",
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_with_remote_definition(only_print: bool) -> None:
    config = docker.buildx.bake(
        print=only_print,
        remote_definition="https://github.com/gabrieldemarmiesse/python-on-whales.git#v0.74.0:tests/python_on_whales/components/bake_tests",
    )
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": "https://github.com/gabrieldemarmiesse/python-on-whales.git#v0.74.0:tests/python_on_whales/components/bake_tests",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image1:1.0.0"],
                "target": "out1",
            },
            "my_out2": {
                "context": "https://github.com/gabrieldemarmiesse/python-on-whales.git#v0.74.0:tests/python_on_whales/components/bake_tests",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:1.0.0"],
                "target": "out2",
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_with_remote_definition_and_target(only_print: bool) -> None:
    config = docker.buildx.bake(
        print=only_print,
        targets=["my_out2"],
        remote_definition="https://github.com/gabrieldemarmiesse/python-on-whales.git#v0.74.0:tests/python_on_whales/components/bake_tests",
    )
    assert config == {
        "group": {"default": {"targets": ["my_out2"]}},
        "target": {
            "my_out2": {
                "context": "https://github.com/gabrieldemarmiesse/python-on-whales.git#v0.74.0:tests/python_on_whales/components/bake_tests",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:1.0.0"],
                "target": "out2",
            },
        },
    }


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
def test_bake_stream_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_NAME_1", "dodo")
    output = docker.buildx.bake(
        files=[bake_file], variables={"TAG": "3.0.4"}, stream_logs=True
    )
    output = list(output)
    assert output[0].startswith("#")
    assert output[-1].startswith("#")


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_metadata_file_output(only_print: bool, tmp_path: Path) -> None:
    metadata_path = tmp_path / "metadata.json"

    config = docker.buildx.bake(
        files=[bake_file], metadata_file=metadata_path, print=only_print
    )
    assert config == {
        "group": {"default": {"targets": ["my_out1", "my_out2"]}},
        "target": {
            "my_out1": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image1:1.0.0"],
                "target": "out1",
            },
            "my_out2": {
                "context": ".",
                "dockerfile": "Dockerfile",
                "tags": ["pretty_image2:1.0.0"],
                "target": "out2",
            },
        },
    }
    if not only_print:
        assert metadata_path.is_file()
        with open(metadata_path) as f:
            metadata = json.load(f)
        assert len(metadata.keys()) == len(
            config["target"]
        )  # two target builds expected
        for target_name, target_metadata in metadata.items():
            assert target_name in config["target"]
            target_tags = config["target"][target_name]["tags"]
            assert isinstance(target_tags, list)
            assert (
                f"docker.io/library/{target_tags[0]}" in target_metadata["image.name"]
            )


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_metadata_file_option(
    only_print: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        env: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return "{}"

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)

    metadata_path = tmp_path / "metadata.json"

    docker.buildx.bake(files=[bake_file], print=only_print, metadata_file=metadata_path)

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" in cmd
    assert metadata_path in cmd or str(metadata_path) in map(str, cmd)


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_without_metadata_file_option(
    only_print: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        env: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return "{}"

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)

    docker.buildx.bake(files=[bake_file], print=only_print)

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" not in cmd


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("change_cwd")
@pytest.mark.parametrize("only_print", [True, False])
def test_bake_metadata_file_str_path_option(
    only_print: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        env: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return "{}"

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)

    metadata_path = tmp_path / "metadata.json"

    docker.buildx.bake(
        files=[bake_file], print=only_print, metadata_file=str(metadata_path)
    )

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" in cmd
    assert metadata_path in cmd or str(metadata_path) in map(str, cmd)


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("prune_all")
def test_prune_all_empty() -> None:
    logs = docker.buildx.prune(all=True, stream_logs=True)
    logs = list(logs)
    assert len(logs) == 1  # nothing reclaimable
    assert logs[0] == "Total:\t0B\n"


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.usefixtures("prune_all")
@pytest.mark.parametrize(
    "filters",
    [
        {"until": "1000000h"},
        [("until", "1000000h")],
        [("until", "1000000h"), ("type", "regular")],
        (pair for pair in [("until", "1000000h")]),
    ],
    ids=["mapping", "pairs", "pairs-two-keys", "generator"],
)
def test_prune_accepts_mapping_and_filter_pairs(
    filters: dict[str, str] | Iterable[tuple[str, str]],
) -> None:
    logs = list(docker.buildx.prune(all=True, filters=filters, stream_logs=True))
    assert logs == ["Total:\t0B\n"]


@pytest.mark.usefixtures("with_docker_driver")
@pytest.mark.parametrize("stream_logs", [False, True])
def test_prune_repeated_keys_are_rejected_by_the_cli(stream_logs: bool) -> None:
    # Repeated keys are forwarded unchanged; buildx reports conflicting until values.
    filters = [("until", "24h"), ("until", "48h")]
    with pytest.raises(DockerException):
        if stream_logs:
            list(docker.buildx.prune(filters=filters, stream_logs=True))
        else:
            docker.buildx.prune(filters=filters)


@pytest.mark.usefixtures("with_docker_driver")
def test_prune(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)

    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(
            tmp_path, cache_from={"type": "local", "src": tmp_path / "cache"}
        )
        logs = list(docker.buildx.prune(all=True, stream_logs=True))
        table_first_row_categories = logs[0]
        table_total_freed_space = logs[-1]
        random_row_with_reclaimable = logs[1]
        assert (
            table_first_row_categories
            == "ID\t\t\t\t\t\tRECLAIMABLE\tSIZE\t\tLAST ACCESSED\n"
        )
        assert "Total:" in table_total_freed_space
        assert "true" in random_row_with_reclaimable
        assert len(logs) >= 2  # table header plus at least one freed file


@pytest.mark.usefixtures("with_docker_driver")
def test_prune_cache_mode_max(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(dockerfile_content1)

    with docker.buildx.create(driver_options={"network": "host"}, use=True):
        docker.buildx.build(
            tmp_path,
            cache_to={"type": "local", "dest": tmp_path / "cache", "mode": "max"},
        )
        logs = list(docker.buildx.prune(all=True, stream_logs=True))
        table_first_row_categories = logs[0]
        table_total_freed_space = logs[-1]
        random_row_with_reclaimable = logs[1]
        assert (
            table_first_row_categories
            == "ID\t\t\t\t\t\tRECLAIMABLE\tSIZE\t\tLAST ACCESSED\n"
        )
        assert "Total:" in table_total_freed_space
        assert "true" in random_row_with_reclaimable
        assert len(logs) >= 2  # table header plus at least one freed file


def test_buildx_build_metadata_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return ""

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)
    monkeypatch.setattr(
        pywhales.components.buildx.cli_wrapper.BuildxCLI,
        "_build_will_load_image",
        lambda self, builder, push, load, output: False,
    )

    metadata_path = tmp_path / "metadata.json"
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    docker.buildx.build(
        context_dir,
        metadata_file=metadata_path,
        push=True,
    )

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" in cmd
    assert metadata_path in cmd or str(metadata_path) in map(str, cmd)


def test_buildx_build_without_metadata_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return ""

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)
    monkeypatch.setattr(
        pywhales.components.buildx.cli_wrapper.BuildxCLI,
        "_build_will_load_image",
        lambda self, builder, push, load, output: False,
    )

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    docker.buildx.build(context_dir)

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" not in cmd


def test_buildx_build_metadata_file_str_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = {}

    def fake_run(
        cmd: Iterable[str],
        capture_stderr: bool = True,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        recorded["cmd"] = list(cmd)
        return ""

    monkeypatch.setattr(pywhales.components.buildx.cli_wrapper, "run", fake_run)
    monkeypatch.setattr(
        pywhales.components.buildx.cli_wrapper.BuildxCLI,
        "_build_will_load_image",
        lambda self, builder, push, load, output: False,
    )

    metadata_path = tmp_path / "metadata.json"
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    docker.buildx.build(
        context_dir,
        metadata_file=str(metadata_path),
    )

    cmd = recorded.get("cmd", [])
    assert "--metadata-file" in cmd
    assert metadata_path in cmd or str(metadata_path) in map(str, cmd)


def test_list() -> None:
    builders = docker.buildx.list()
    assert docker.buildx.inspect() in builders
    assert docker.buildx.inspect("default") in builders
    for builder in builders:
        assert builder.driver in ["docker", "docker-container"]


@pytest.mark.parametrize("strict", [False, True])
def test_build_arguments_reach_report_child_environment(
    strict: bool, tmp_path: Path
) -> None:
    record = tmp_path / "child.json"
    script = (
        "import json, os, pathlib, sys; "
        f"pathlib.Path({str(record)!r}).write_text(json.dumps([sys.argv[1:], os.environ['APP_REPORT_ARG']]))"
    )
    client = DockerClient(
        client_call=[sys.executable, "-c", script], strict_env_transport=strict
    )

    client.buildx.build(
        ".", call="check", build_args={"APP_REPORT_ARG": "private-value"}
    )

    argv, value = json.loads(record.read_text())
    assert argv[argv.index("--build-arg") + 1] == "APP_REPORT_ARG"
    assert all("private-value" not in arg for arg in argv)
    assert value == "private-value"


@pytest.mark.parametrize("consume", [False, True])
def test_owned_buildx_logs_cancel_idle_child(consume: bool) -> None:
    stream = pywhales.components.buildx.cli_wrapper.stream_buildx_logs(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ]
    )
    with stream:
        if consume:
            assert next(stream) == "ready\n"

        stream.close()
        stream.close()

        assert list(stream) == []
        assert stream._stream._process.poll() is not None
        assert all(not thread.is_alive() for thread in stream._stream._threads)


def test_owned_buildx_logs_context_preserves_body_error() -> None:
    stream = pywhales.components.buildx.cli_wrapper.stream_buildx_logs(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    error = RuntimeError("log consumer failed")

    with pytest.raises(RuntimeError) as raised, stream:
        raise error

    assert raised.value is error
    assert stream._stream._process.poll() is not None


def test_owned_buildx_logs_eof_preserves_decode_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STREAM_ENV", "inherited")
    stream = pywhales.components.buildx.cli_wrapper.stream_buildx_logs(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, os.environ['STREAM_ENV'].encode() + b'\\n'); "
            "os.write(2, b'\\xe2\\x9e\\n')",
        ],
        env={"STREAM_ENV": "override"},
    )

    output = list(stream)

    assert sorted(output) == sorted(["override\n", "\ufffd\n"])
    assert stream._stream._process.poll() == 0


def test_owned_buildx_logs_eof_preserves_failure() -> None:
    stream = pywhales.components.buildx.cli_wrapper.stream_buildx_logs(
        [
            sys.executable,
            "-c",
            "import sys; print('progress'); sys.stderr.write('No such image: missing\\n'); sys.exit(9)",
        ]
    )
    output: list[str] = []

    with pytest.raises(NoSuchImage) as raised, stream:
        output.extend(stream)

    assert "progress\n" in output
    assert "No such image: missing\n" in output
    assert raised.value.return_code == 9
    assert raised.value.stderr == "No such image: missing\n"
    assert stream._stream._process.poll() == 9


@pytest.mark.parametrize("operation", ["build", "bake", "prune"])
def test_owned_buildx_api_propagates_stream_ownership(
    operation: Literal["build", "bake", "prune"],
) -> None:
    with (
        stream_stdout_and_stderr(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        ) as raw,
        patch(
            "pywhales.components.buildx.cli_wrapper.stream_stdout_and_stderr",
            return_value=raw,
        ) as start,
    ):
        match operation:
            case "build":
                mapped = docker.buildx.build(".", stream_logs=True)
            case "bake":
                mapped = docker.buildx.bake(
                    targets=["target"],
                    variables={"STREAM_ENV": "bake"},
                    stream_logs=True,
                )
                assert start.call_args.kwargs["env"] == {"STREAM_ENV": "bake"}
                assert start.call_args.args[0][-1] == "target"
            case "prune":
                mapped = docker.buildx.prune(stream_logs=True)
            case unreachable:
                assert_never(unreachable)

        with mapped:
            pass

        assert raw._process.poll() is not None
        assert all(not thread.is_alive() for thread in raw._threads)
