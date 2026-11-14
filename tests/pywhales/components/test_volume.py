import inspect
import json
import math
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal
from unittest.mock import patch

import pytest
from pytest_mock import MockerFixture

from pywhales import Container, DockerClient, Image, Volume
from pywhales._execution_policy import get_execution_policy
from pywhales.client_config import ParsingError
from pywhales.components.buildx.cli_wrapper import BuildxCLI
from pywhales.components.container.cli_wrapper import ContainerCLI
from pywhales.components.image.cli_wrapper import ImageCLI
from pywhales.components.volume import cli_wrapper as volume_cli
from pywhales.components.volume.models import VolumeInspectResult
from pywhales.exceptions import (
    DockerException,
    NoSuchContainer,
    NoSuchImage,
    NoSuchNetwork,
    NoSuchVolume,
)
from pywhales.test_utils import get_all_jsons, random_name
from tests.pywhales.components.hydration_fakes import (
    FakeCli,
    fake_daemon,
    identity_of,
)


def test_batch_inspect_volume_names_and_typed_snapshot() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    snapshots = [
        {"Name": name, "Driver": "local", "Mountpoint": f"/data/{name}"}
        for name in ["second", "first", "second"]
    ]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps(snapshots).encode(), b""),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        volumes = client.volume.inspect(["second", "first", "second"])
        assert [volume.name for volume in volumes] == ["second", "first", "second"]
        assert [volume.mountpoint for volume in volumes] == [
            Path("/data/second"),
            Path("/data/first"),
            Path("/data/second"),
        ]
        assert all(
            isinstance(volume._inspect_result, VolumeInspectResult)
            for volume in volumes
        )
        assert command.call_count == 1


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_volume_cleanup_preserves_removal(missing: bool) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    volume = Volume(client.client_config, "resource", is_immutable_id=True)
    response = CompletedProcess(
        [], 1 if missing else 0, b"", b"No such volume: resource" if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, volume:
        pass
    assert command.call_args.args[0] == ["docker-test", "volume", "remove", "resource"]
    assert command.call_count == 1
    if missing:
        with (
            patch("subprocess.run", return_value=response),
            pytest.raises(NoSuchVolume),
        ):
            volume.remove()


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        pytest.param(b"permission denied", DockerException, id="command-failure"),
        pytest.param(b"No such container: resource", NoSuchContainer, id="container"),
        pytest.param(b"No such image: resource", NoSuchImage, id="image"),
        pytest.param(b"network resource not found", NoSuchNetwork, id="network"),
    ],
)
def test_volume_cleanup_propagates_unrelated_cli_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    volume = Volume(client.client_config, "resource", is_immutable_id=True)
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 17, b"output", stderr)
        ) as command,
        pytest.raises(DockerException) as raised,
        volume,
    ):
        pass

    expected = ["docker-test", "volume", "remove", "resource"]
    command.assert_called_once()
    assert command.call_args.args[0] == expected
    assert type(raised.value) is error_type
    assert raised.value.docker_command == expected
    assert raised.value.return_code == 17
    assert raised.value.stdout == "output"
    assert raised.value.stderr == stderr.decode()


class _Interrupted(BaseException):
    pass


type HelperStep = Literal["build", "create", "copy", "remove_container", "remove_image"]
type Launch = tuple[HelperStep, float | None, dict[str, object]]


def _command_failure(label: str) -> DockerException:
    return DockerException([label], 1, b"", label.encode())


def _patch_helpers(
    mocker: MockerFixture,
    client: DockerClient,
    failures: dict[HelperStep, BaseException],
) -> list[Launch]:
    """Replace helper launches, recording each step and the scoped default deadline."""
    config = client.client_config
    config._client_call_with_path = ["docker-test"]
    launches: list[Launch] = []

    def record(step: HelperStep, **kwargs: object) -> None:
        scoped = get_execution_policy(config.docker_cmd).default_timeout
        launches.append((step, scoped, kwargs))
        if step in failures:
            raise failures[step]

    def build(self: object, context: object, **kwargs: object) -> Image:
        record("build", **kwargs)
        return Image(config, "image-id", is_immutable_id=True)

    def create(self: object, image: object, **kwargs: object) -> Container:
        record("create", **kwargs)
        return Container(config, "container-id", is_immutable_id=True)

    def copy(
        self: object,
        source: object,
        destination: object,
        *,
        timeout: float | None = None,
    ) -> None:
        record("copy", source=source, destination=destination, timeout=timeout)

    def remove_container(self: object, containers: object, **kwargs: object) -> None:
        record("remove_container", target=containers, **kwargs)

    def remove_image(self: object, x: object, **kwargs: object) -> None:
        record("remove_image", target=x, **kwargs)

    mocker.patch.object(BuildxCLI, "build", build)
    mocker.patch.object(ContainerCLI, "create", create)
    mocker.patch.object(ContainerCLI, "copy", copy)
    mocker.patch.object(ContainerCLI, "remove", remove_container)
    mocker.patch.object(ImageCLI, "remove", remove_image)
    return launches


def _steps(launches: Sequence[Launch]) -> list[HelperStep]:
    return [step for step, _, _ in launches]


def _helper_names(launches: Sequence[Launch]) -> tuple[object, object]:
    by_step = {step: kwargs for step, _, kwargs in launches}
    return by_step["create"]["name"], by_step["build"]["tags"]


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("to_volume", [False, True], ids=["from-volume", "to-volume"])
def test_volume_copy_forwards_timeout_to_every_launch(
    mocker: MockerFixture, to_volume: bool, timeout: float | None
) -> None:
    client = DockerClient()
    launches = _patch_helpers(mocker, client, {})
    endpoints: tuple[object, object] = (
        ("local", ("data", "sub")) if to_volume else (("data", "sub"), "local")
    )

    if timeout is None:
        client.volume.copy(*endpoints)
    else:
        client.volume.copy(*endpoints, timeout=timeout)

    assert _steps(launches) == [
        "build",
        "create",
        "copy",
        "remove_container",
        "remove_image",
    ]
    container_name, image_name = _helper_names(launches)
    by_step = {step: kwargs for step, _, kwargs in launches}
    assert by_step["build"]["timeout"] == timeout
    assert by_step["copy"]["timeout"] == timeout
    assert by_step["remove_container"]["target"] == container_name
    assert by_step["remove_image"]["target"] == image_name
    assert by_step["create"]["volumes"] == [("data", Path("/volume"))]
    assert {scoped for _, scoped, _ in launches} == {timeout}
    assert get_execution_policy(client.client_config.docker_cmd).default_timeout is None


@pytest.mark.parametrize("timeout", [True, 0, 0.0, -1.0, math.nan, math.inf])
@pytest.mark.parametrize("operation", ["copy", "clone", "handle_clone"])
def test_volume_transfer_rejects_invalid_timeout_before_any_launch(
    mocker: MockerFixture,
    operation: Literal["copy", "clone", "handle_clone"],
    timeout: float,
) -> None:
    client = DockerClient()
    launches = _patch_helpers(mocker, client, {})
    run = mocker.patch.object(volume_cli, "run")

    with pytest.raises(ValueError, match="timeout"):
        match operation:
            case "copy":
                client.volume.copy(("data", "."), "local", timeout=timeout)
            case "clone":
                client.volume.clone("data", timeout=timeout)
            case "handle_clone":
                Volume(client.client_config, "data", is_immutable_id=True).clone(
                    timeout=timeout
                )

    assert launches == []
    run.assert_not_called()


def test_volume_copy_without_a_volume_endpoint_launches_nothing(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    launches = _patch_helpers(mocker, client, {})

    with pytest.raises(ValueError, match="tuple"):
        client.volume.copy("here", "there")

    assert launches == []


@pytest.mark.parametrize(
    ("failing", "removed"),
    [
        pytest.param("build", ["remove_image"], id="build"),
        pytest.param("create", ["remove_container", "remove_image"], id="create"),
        pytest.param("copy", ["remove_container", "remove_image"], id="copy"),
    ],
)
@pytest.mark.parametrize("error_type", [DockerException, _Interrupted])
def test_volume_copy_removes_helpers_by_chosen_names_after_failure(
    mocker: MockerFixture,
    failing: Literal["build", "create", "copy"],
    removed: list[HelperStep],
    error_type: type[BaseException],
) -> None:
    client = DockerClient()
    failure = (
        _command_failure("body") if error_type is DockerException else error_type()
    )
    launches = _patch_helpers(mocker, client, {failing: failure})

    with pytest.raises(error_type) as raised:
        client.volume.copy(("data", "."), "local", timeout=4.0)

    assert raised.value is failure
    attempted = _steps(launches)
    assert attempted[-len(removed) :] == removed
    by_step = {step: kwargs for step, _, kwargs in launches}
    if "create" in by_step:
        assert by_step["remove_container"]["target"] == by_step["create"]["name"]
    assert by_step["remove_image"]["target"] == by_step["build"]["tags"]
    assert "remove_container" in by_step or failing == "build"
    assert {scoped for _, scoped, _ in launches} == {4.0}


def test_volume_copy_ignores_missing_helpers_after_failure(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    failure = _command_failure("body")
    launches = _patch_helpers(
        mocker,
        client,
        {
            "copy": failure,
            "remove_container": NoSuchContainer(["rm"], 1, b"", b"No such container"),
            "remove_image": NoSuchImage(["rmi"], 1, b"", b"No such image"),
        },
    )

    with pytest.raises(DockerException) as raised:
        client.volume.copy(("data", "."), "local")

    assert raised.value is failure
    assert _steps(launches)[-2:] == ["remove_container", "remove_image"]


def test_volume_copy_ignores_missing_helpers_after_success(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    launches = _patch_helpers(
        mocker,
        client,
        {
            "remove_container": NoSuchContainer(["rm"], 1, b"", b"No such container"),
            "remove_image": NoSuchImage(["rmi"], 1, b"", b"No such image"),
        },
    )

    client.volume.copy(("data", "."), "local")

    assert _steps(launches)[-2:] == ["remove_container", "remove_image"]


def test_volume_copy_attempts_image_removal_after_container_removal_fails(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    container_failure = _command_failure("container")
    launches = _patch_helpers(mocker, client, {"remove_container": container_failure})

    with pytest.raises(DockerException) as raised:
        client.volume.copy(("data", "."), "local")

    assert raised.value is container_failure
    assert _steps(launches)[-2:] == ["remove_container", "remove_image"]


def test_volume_copy_keeps_cleanup_failures_when_copy_succeeded(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    container_failure = _command_failure("container")
    image_failure = _command_failure("image")
    launches = _patch_helpers(
        mocker,
        client,
        {"remove_container": container_failure, "remove_image": image_failure},
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        client.volume.copy(("data", "."), "local")

    assert raised.value.exceptions == (container_failure, image_failure)
    assert _steps(launches)[-2:] == ["remove_container", "remove_image"]


@pytest.mark.parametrize("error_type", [DockerException, _Interrupted])
def test_volume_copy_keeps_body_and_both_removal_failures(
    mocker: MockerFixture, error_type: type[BaseException]
) -> None:
    client = DockerClient()
    body = _command_failure("body") if error_type is DockerException else error_type()
    container_failure = _command_failure("container")
    image_failure = _command_failure("image")
    launches = _patch_helpers(
        mocker,
        client,
        {
            "copy": body,
            "remove_container": container_failure,
            "remove_image": image_failure,
        },
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        client.volume.copy(("data", "."), "local")

    outer = raised.value
    assert len(outer.exceptions) == 2
    inner, last = outer.exceptions
    assert last is image_failure
    assert isinstance(inner, BaseExceptionGroup)
    assert inner.exceptions == (body, container_failure)
    assert _steps(launches)[-2:] == ["remove_container", "remove_image"]


def test_volume_copy_keeps_body_and_image_removal_failure(
    mocker: MockerFixture,
) -> None:
    client = DockerClient()
    body = _command_failure("body")
    image_failure = _command_failure("image")
    _patch_helpers(mocker, client, {"build": body, "remove_image": image_failure})

    with pytest.raises(BaseExceptionGroup) as raised:
        client.volume.copy(("data", "."), "local")

    assert raised.value.exceptions == (body, image_failure)


def _patch_clone(
    mocker: MockerFixture, client: DockerClient, copy_failure: BaseException | None
) -> tuple[list[tuple[list[str], float | None]], list[dict[str, object]]]:
    """Record volume launches with their scoped deadline, and stub out copies."""
    config = client.client_config
    config._client_call_with_path = ["docker-test"]
    launches: list[tuple[list[str], float | None]] = []
    copies: list[dict[str, object]] = []

    def run(command: Sequence[object]) -> str:
        scoped = get_execution_policy(command).default_timeout
        launches.append(([str(part) for part in command], scoped))
        return "destination" if "create" in command else '[{"Name": "destination"}]'

    def copy(
        self: object, source: object, destination: object, **kwargs: object
    ) -> None:
        copies.append(
            {"source": source, "destination": destination, **kwargs}
            | {"scoped": get_execution_policy(config.docker_cmd).default_timeout}
        )
        if copy_failure is not None and len(copies) == 2:
            raise copy_failure

    mocker.patch.object(volume_cli, "run", run)
    mocker.patch.object(volume_cli.VolumeCLI, "copy", copy)
    return launches, copies


@pytest.mark.parametrize("timeout", [None, 3.5])
@pytest.mark.parametrize("via_handle", [False, True], ids=["cli", "handle"])
def test_volume_clone_bounds_creation_inspection_and_both_copies(
    mocker: MockerFixture, via_handle: bool, timeout: float | None
) -> None:
    client = DockerClient()
    launches, copies = _patch_clone(mocker, client, None)
    source = Volume(client.client_config, "source", is_immutable_id=True)

    if via_handle:
        result = source.clone("destination", timeout=timeout)
    else:
        result = client.volume.clone(source, "destination", timeout=timeout)

    assert [command[1:3] for command, _ in launches] == [
        ["volume", "create"],
        ["volume", "inspect"],
    ]
    assert {scoped for _, scoped in launches} == {timeout}
    assert [copy["timeout"] for copy in copies] == [timeout, timeout]
    assert {copy["scoped"] for copy in copies} == {timeout}
    assert copies[0]["source"] == (source, ".")
    assert copies[1]["destination"] == (result, "")
    assert result.client_config is client.client_config
    assert result.name == "destination"


@pytest.mark.parametrize("error_type", [DockerException, _Interrupted])
def test_volume_clone_keeps_destination_volume_when_copy_fails(
    mocker: MockerFixture, error_type: type[BaseException]
) -> None:
    client = DockerClient()
    failure = (
        _command_failure("copy") if error_type is DockerException else error_type()
    )
    launches, copies = _patch_clone(mocker, client, failure)

    with pytest.raises(error_type) as raised:
        client.volume.clone("source", "destination", timeout=1.5)

    assert raised.value is failure
    assert len(copies) == 2
    assert all(command[1:3] != ["volume", "remove"] for command, _ in launches)


@pytest.mark.parametrize("json_file", get_all_jsons("volumes"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    a = VolumeInspectResult(**json.loads(json_as_txt))
    if json_file.name == "5.json":
        assert a.created_at == datetime(2026, 11, 14, 13, 6, 4, 961510, tzinfo=UTC)
        assert a.mountpoint is None
        assert a.cluster_volume is not None
        assert a.cluster_volume["ID"] == "8r0nfpsau6z9bkmfipkrwtlmy"
    if json_file.name == "0.json":
        assert a.created_at == datetime(
            year=2020,
            month=8,
            day=27,
            hour=9,
            minute=39,
            second=50,
            tzinfo=timezone(timedelta(hours=2)),
        )
        assert a.mountpoint == Path(
            "/var/lib/docker/volumes/dodo_letsencrypt_config/_data"
        )
        assert a.options is None


def test_simple_volume(ctr_client: DockerClient) -> None:
    some_volume = ctr_client.volume.create()
    assert some_volume.exists()
    assert some_volume.name in repr(ctr_client.volume.list())
    assert some_volume.driver is not None
    assert some_volume.driver in repr(ctr_client.volume.list())
    ctr_client.volume.remove(some_volume)


def test_multiple_volumes(ctr_client: DockerClient) -> None:
    volumes: list[Volume | str] = [ctr_client.volume.create() for _ in range(3)]

    for v in volumes:
        assert v in ctr_client.volume.list()

    ctr_client.volume.remove(volumes)

    for v in volumes:
        assert v not in ctr_client.volume.list()


def test_volume_drivers(ctr_client: DockerClient) -> None:
    some_volume = ctr_client.volume.create(
        driver="local",
        options={"type": "tmpfs", "device": "tmpfs", "o": "size=100m,uid=1000"},
    )
    ctr_client.run(
        "busybox",
        ["touch", "/dodo/dada"],
        volumes=[(some_volume, "/dodo")],
        remove=True,
    )
    ctr_client.volume.remove(some_volume)


def test_volume_labels(ctr_client: DockerClient) -> None:
    some_volume = ctr_client.volume.create(labels={"dodo": "dada"})

    assert some_volume.labels is not None
    assert some_volume.labels["dodo"] == "dada"
    ctr_client.volume.remove(some_volume)


def test_list(ctr_client: DockerClient) -> None:
    volumes = [ctr_client.volume.create() for _ in range(3)]

    all_volumes = ctr_client.volume.list()
    for v in volumes:
        assert v in all_volumes


def test_list_filters(ctr_client: DockerClient) -> None:
    select_label = random_name()
    with (
        ctr_client.volume.create(),
        ctr_client.volume.create(labels={select_label: ""}) as vol2,
    ):
        listed_volumes = ctr_client.volume.list(filters=[("label", select_label)])
        assert listed_volumes == [vol2]


def test_list_filters_client(docker_client: DockerClient) -> None:
    select_label = random_name()
    with (
        docker_client.volume.create(),
        docker_client.volume.create(labels={select_label: ""}) as vol2,
    ):
        listed_volumes = docker_client.volume.list(filters=[("label", select_label)])
        assert listed_volumes == [vol2]


def test_copy_to_volume(ctr_client: DockerClient, tmp_path: Path) -> None:
    some_volume = ctr_client.volume.create()
    ctr_client.run(
        "busybox",
        ["touch", "/volume/dodo.txt"],
        remove=True,
        volumes=[(some_volume, "/volume")],
    )

    ctr_client.volume.copy((some_volume, "dodo.txt"), tmp_path)
    assert (tmp_path / "dodo.txt").exists()


def test_copy_to_volume_subdirectory(ctr_client: DockerClient, tmp_path: Path) -> None:
    some_volume = ctr_client.volume.create()
    ctr_client.run(
        "busybox",
        [
            "sh",
            "-c",
            "mkdir -p /volume/subdir/subdir2 && touch /volume/subdir/subdir2/dodo.txt",
        ],
        remove=True,
        volumes=[(some_volume, "/volume")],
    )

    ctr_client.volume.copy((some_volume, "subdir/subdir2"), tmp_path)
    assert (tmp_path / "subdir2/dodo.txt").exists()


def test_copy_to_and_from_volume(ctr_client: DockerClient, tmp_path: Path) -> None:
    some_volume = ctr_client.volume.create()

    text_file = tmp_path / "some_dir" / "dodo.txt"
    text_file.parent.mkdir(parents=True)
    text_file.write_text("Hello\nWorld!")
    ctr_client.volume.copy(tmp_path, (some_volume, "subdir"))

    text_file.unlink()
    ctr_client.volume.copy((some_volume, "subdir/"), tmp_path)

    assert (tmp_path / "subdir/some_dir/dodo.txt").read_text() == "Hello\nWorld!"


def test_volume_cp_from_in_dir(ctr_client: DockerClient, tmp_path: Path) -> None:
    some_volume = ctr_client.volume.create()
    ctr_client.run(
        "busybox",
        ["touch", "/volume/dodo.txt"],
        remove=True,
        volumes=[(some_volume, "/volume")],
    )

    ctr_client.volume.copy((some_volume, "."), tmp_path)
    assert (tmp_path / "dodo.txt").exists()


def test_volume_cp_to_in_dir(ctr_client: DockerClient, tmp_path: Path) -> None:
    dodo = tmp_path / "dada" / "dodo.txt"
    dodo.parent.mkdir()
    dodo.touch()
    some_volume = ctr_client.volume.create()
    ctr_client.volume.copy(str(tmp_path) + "/.", (some_volume, ""))
    files = ctr_client.run(
        "busybox",
        ["ls", "/volume/dada/"],
        remove=True,
        volumes=[(some_volume, "/volume")],
    )

    assert files == "dodo.txt"


def test_clone(ctr_client: DockerClient) -> None:
    some_volume = ctr_client.volume.create()
    ctr_client.run(
        "busybox",
        [
            "sh",
            "-c",
            "mkdir -p /volume/subdir/subdir2 && touch /volume/subdir/subdir2/dodo.txt",
        ],
        remove=True,
        volumes=[(some_volume, "/volume")],
    )

    new_volume = ctr_client.volume.clone(some_volume)
    files = ctr_client.run(
        "busybox",
        [
            "sh",
            "-c",
            "ls /volume/subdir/subdir2",
        ],
        remove=True,
        volumes=[(new_volume, "/volume")],
    )
    assert files == "dodo.txt"


@pytest.mark.parametrize("method", ["inspect", "remove"])
def test_functions_no_such_volume(ctr_client: DockerClient, method: str) -> None:
    with pytest.raises(NoSuchVolume) as e:
        getattr(ctr_client.volume, method)("dodo")
    assert e.value.stderr is not None
    assert "no such volume" in e.value.stderr.lower()


def test_volume_does_not_exists(ctr_client: DockerClient) -> None:
    assert not ctr_client.volume.exists("dodo")


def test_volume_exists(ctr_client: DockerClient) -> None:
    with ctr_client.volume.create() as v:
        assert v.exists()
        assert ctr_client.volume.exists(v.name)


def test_prune(ctr_client: DockerClient) -> None:
    for volume in ctr_client.volume.list(filters=[("name", "test-volume")]):
        volume.remove()
    volume = ctr_client.volume.create("test-volume")
    assert volume in ctr_client.volume.list()

    # volume not pruned because it does not have label "dne"
    ctr_client.volume.prune(filters=[("label", "dne")], all=True)
    assert volume in ctr_client.volume.list()

    # could only find "label" filter for `docker volume prune`

    # volume pruned
    ctr_client.volume.prune(all=True)
    assert volume not in ctr_client.volume.list()


def test_volume_remove_empty_list(ctr_client: DockerClient) -> None:
    with ctr_client.volume.create() as my_volume:
        assert my_volume in ctr_client.volume.list()
        all_volumes = set(ctr_client.volume.list())
        ctr_client.volume.remove([])
        assert all_volumes == set(ctr_client.volume.list())


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
def test_volume_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.volume.remove(make_empty())


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_volume_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.volume.remove(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "volume", "remove", "alpha"]


def _volume_ids(count: int) -> list[str]:
    return [identity_of("volume", index) for index in range(count)]


def test_hydrated_volume_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _volume_ids(5)
    cli = FakeCli("volume", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        resources = client.volume.list(hydrate=True)
        launches = len(cli.launches)
        assert [str(resource) for resource in resources] == ids
        assert all(resource.driver is not None for resource in resources)
    assert launches == 4
    assert len(cli.launches) == launches
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]


def test_default_volume_list_stays_lazy_and_expiring() -> None:
    ids = _volume_ids(3)
    cli = FakeCli("volume", ids)
    with fake_daemon(cli) as client:
        resources = client.volume.list()
        assert cli.inspects() == []
        assert resources[0].driver is not None
        assert resources[0].driver is not None
        hydrate = inspect.signature(client.volume.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.inspects() == [[ids[0]], [ids[0]]]


def test_hydrated_volume_list_reprobes_only_the_chunk_with_a_vanished_resource() -> (
    None
):
    ids = _volume_ids(4)
    cli = FakeCli("volume", ids)
    cli.vanish_on_list = {ids[2]}
    with fake_daemon(cli, chunk_size=2) as client:
        resources = client.volume.list(hydrate=True)
        launches = len(cli.launches)
        assert [str(resource) for resource in resources] == [ids[0], ids[1], ids[3]]
        assert all(resource.driver is not None for resource in resources)
    assert len(cli.launches) == launches
    assert cli.inspects() == [ids[:2], ids[2:], [ids[2]], [ids[3]]]


def test_hydrated_volume_list_matches_results_by_exact_identity() -> None:
    ids = _volume_ids(3)
    cli = FakeCli("volume", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for resource in client.volume.list(hydrate=True):
            assert resource._inspect_result is not None
            assert resource._inspect_result.name == str(resource)
        cli.corrupt = {ids[1]: "other"}
        with pytest.raises(ParsingError):
            client.volume.list(hydrate=True)


def test_reload_and_mutating_calls_release_the_pin_of_a_hydrated_volume() -> None:
    ids = _volume_ids(3)
    cli = FakeCli("volume", ids)
    with fake_daemon(cli) as client:
        reloaded, removed, untouched = client.volume.list(hydrate=True)
        before = len(cli.launches)
        assert None not in (reloaded.driver, removed.driver, untouched.driver)
        assert len(cli.launches) == before
        reloaded.reload()
        assert reloaded.driver is not None
        assert len(cli.launches) == before + 2
        assert untouched.driver is not None
        assert len(cli.launches) == before + 2
        removed.remove()
        used = len(cli.launches)
        assert removed.driver is not None
        assert len(cli.launches) == used + 1
