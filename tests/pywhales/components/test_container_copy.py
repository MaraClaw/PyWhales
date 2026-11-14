import math
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypedDict, Unpack, assert_never, get_type_hints

import pytest

import pywhales.components.container.cli_wrapper as container_cli
from pywhales import DockerClient
from pywhales.client_config import ClientConfig
from pywhales.components.container.cli_wrapper import Container, ContainerPath
from pywhales.utils import ValidPath


@pytest.mark.parametrize("parameter", ["source", "destination"])
def test_copy_parameters_accept_only_paths(parameter: str) -> None:
    hints = get_type_hints(container_cli.ContainerCLI.copy)

    assert hints[parameter] == ValidPath | ContainerPath


@pytest.mark.parametrize(
    ("source", "destination", "expected"),
    [
        (
            "local file.txt",
            ("example", "/tmp/in file.txt"),
            ("local file.txt", "example:/tmp/in file.txt"),
        ),
        (
            Path("local file.txt"),
            ("example", Path("/tmp/in file.txt")),
            ("local file.txt", "example:/tmp/in file.txt"),
        ),
        (
            ("example", "/tmp/out file.txt"),
            "local file.txt",
            ("example:/tmp/out file.txt", "local file.txt"),
        ),
        (
            ("example", Path("/tmp/out file.txt")),
            Path("local file.txt"),
            ("example:/tmp/out file.txt", "local file.txt"),
        ),
        (
            "example:/tmp/raw file.txt",
            Path("local file.txt"),
            ("example:/tmp/raw file.txt", "local file.txt"),
        ),
        (
            Path("local file.txt"),
            "example:/tmp/raw file.txt",
            ("local file.txt", "example:/tmp/raw file.txt"),
        ),
        (
            "local file.txt",
            (
                Container(ClientConfig(), "immutable-id", is_immutable_id=True),
                Path("/tmp/in file.txt"),
            ),
            ("local file.txt", "immutable-id:/tmp/in file.txt"),
        ),
        (
            (
                Container(ClientConfig(), "immutable-id", is_immutable_id=True),
                Path("/tmp/out file.txt"),
            ),
            Path("local file.txt"),
            ("immutable-id:/tmp/out file.txt", "local file.txt"),
        ),
    ],
)
def test_copy_preserves_path_arguments(
    monkeypatch: pytest.MonkeyPatch,
    source: ValidPath | ContainerPath,
    destination: ValidPath | ContainerPath,
    expected: tuple[str, str],
) -> None:
    commands: list[Sequence[str | Path]] = []

    def run(command: Sequence[str | Path], *, timeout: float | None = None) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(container_cli, "run", run)
    client = DockerClient(
        client_config=ClientConfig(_client_call_with_path=["container-cli"])
    )

    client.copy(source, destination)

    assert commands == [["container-cli", "container", "cp", *expected]]


type TransferOperation = Literal[
    "copy",
    "export",
    "cli_copy",
    "cli_export",
    "copy_from",
    "copy_to",
    "handle_export",
]

TRANSFER_OPERATIONS: tuple[TransferOperation, ...] = (
    "copy",
    "export",
    "cli_copy",
    "cli_export",
    "copy_from",
    "copy_to",
    "handle_export",
)

TRANSFER_COMMANDS: dict[TransferOperation, list[str]] = {
    "copy": ["container-cli", "container", "cp", "local.txt", "immutable-id:/in.txt"],
    "export": ["container-cli", "container", "export", "--output", "out.tar", "name"],
    "cli_copy": [
        "container-cli",
        "container",
        "cp",
        "immutable-id:/out.txt",
        "local.txt",
    ],
    "cli_export": [
        "container-cli",
        "container",
        "export",
        "--output",
        "out.tar",
        "immutable-id",
    ],
    "copy_from": [
        "container-cli",
        "container",
        "cp",
        "immutable-id:/out.txt",
        "local.txt",
    ],
    "copy_to": [
        "container-cli",
        "container",
        "cp",
        "local.txt",
        "immutable-id:/in.txt",
    ],
    "handle_export": [
        "container-cli",
        "container",
        "export",
        "--output",
        "out.tar",
        "immutable-id",
    ],
}


class _TimeoutOverride(TypedDict, total=False):
    timeout: float | None


def _transfer(
    client: DockerClient,
    operation: TransferOperation,
    **timeout: Unpack[_TimeoutOverride],
) -> None:
    handle = Container(client.client_config, "immutable-id", is_immutable_id=True)
    match operation:
        case "copy":
            client.copy("local.txt", (handle, "/in.txt"), **timeout)
        case "export":
            client.export("name", "out.tar", **timeout)
        case "cli_copy":
            client.container.copy((handle, "/out.txt"), "local.txt", **timeout)
        case "cli_export":
            client.container.export(handle, "out.tar", **timeout)
        case "copy_from":
            handle.copy_from("/out.txt", "local.txt", **timeout)
        case "copy_to":
            handle.copy_to("local.txt", "/in.txt", **timeout)
        case "handle_export":
            handle.export("out.tar", **timeout)
        case unreachable:
            assert_never(unreachable)


@pytest.mark.parametrize("timeout", [None, 2.5])
@pytest.mark.parametrize("operation", TRANSFER_OPERATIONS)
def test_transfer_forwards_timeout_and_preserves_arguments(
    monkeypatch: pytest.MonkeyPatch,
    operation: TransferOperation,
    timeout: float | None,
) -> None:
    launches: list[tuple[list[str], float | None]] = []

    def run(command: Sequence[str | Path], *, timeout: float | None = None) -> str:
        launches.append(([str(part) for part in command], timeout))
        return ""

    monkeypatch.setattr(container_cli, "run", run)
    client = DockerClient(
        client_config=ClientConfig(_client_call_with_path=["container-cli"])
    )

    if timeout is None:
        _transfer(client, operation)
    else:
        _transfer(client, operation, timeout=timeout)

    assert launches == [(TRANSFER_COMMANDS[operation], timeout)]


@pytest.mark.parametrize("timeout", [True, 0, 0.0, -1.0, math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("operation", TRANSFER_OPERATIONS)
def test_transfer_rejects_invalid_timeout_before_resolving_cli(
    monkeypatch: pytest.MonkeyPatch,
    operation: TransferOperation,
    timeout: float,
) -> None:
    def run(command: Sequence[str | Path], *, timeout: float | None = None) -> str:
        raise AssertionError("CLI launched")

    monkeypatch.setattr(container_cli, "run", run)
    config = ClientConfig(client_call=["pywhales-missing-container-cli"])
    client = DockerClient(client_config=config)

    with pytest.raises(ValueError, match="timeout"):
        _transfer(client, operation, timeout=timeout)

    assert config._client_call_with_path is None
