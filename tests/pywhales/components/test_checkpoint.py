"""Checkpoint command group asserted as exact argv; never launches Docker."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.components.checkpoint.cli_wrapper import CheckpointCLI
from pywhales.exceptions import DockerException, DockerTimeout
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

CONTAINER_ID = "a" * 64
SIZED_INSPECT = json.dumps(
    [{"Id": CONTAINER_ID, "Name": "/web", "SizeRw": 1024, "SizeRootFs": 4096}]
)
PLAIN_INSPECT = json.dumps([{"Id": CONTAINER_ID, "Name": "/web"}])
EXPERIMENTAL = "checkpoint is only supported on a Docker daemon with experimental"


def checkpoints(client: DockerClient) -> CheckpointCLI:
    return client.checkpoint


def test_checkpoint_group_is_wired_with_the_shared_config(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path)
    assert isinstance(client.checkpoint, CheckpointCLI)
    assert client.checkpoint.client_config is client.client_config


def test_create_emits_options_before_the_positionals(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    checkpoints(client).create("web", "cp1")
    checkpoints(client).create(
        "web", "cp1", checkpoint_dir=tmp_path / "cp", leave_running=True, timeout=5
    )
    assert calls(log) == [
        ["checkpoint", "create", "web", "cp1"],
        [
            "checkpoint",
            "create",
            "--checkpoint-dir",
            str(tmp_path / "cp"),
            "--leave-running",
            "web",
            "cp1",
        ],
    ]


def test_create_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        checkpoints(client).create("web", "cp1", timeout=0)
    assert calls(log) == []


def test_create_deadline_covers_the_local_cli(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("checkpoint",), sleep=30)])
    with pytest.raises(DockerTimeout):
        checkpoints(client).create("web", "cp1", timeout=0.3)


def test_list_skips_the_heading(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("ls",), stdout="CHECKPOINT NAME\ncp1\ncp2\n\n")]
    )
    assert checkpoints(client).list("web") == ["cp1", "cp2"]
    assert checkpoints(client).list("web", checkpoint_dir="/var/cp") == ["cp1", "cp2"]
    assert calls(log) == [
        ["checkpoint", "ls", "web"],
        ["checkpoint", "ls", "--checkpoint-dir", "/var/cp", "web"],
    ]


@pytest.mark.parametrize("stdout", ["", "CHECKPOINT NAME\n"])
def test_list_without_checkpoints_is_empty(tmp_path: Path, stdout: str) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("ls",), stdout=stdout)])
    assert checkpoints(client).list("web") == []


def test_remove_emits_options_before_the_positionals(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    checkpoints(client).remove("web", "cp1")
    checkpoints(client).remove("web", "cp1", checkpoint_dir=tmp_path / "cp")
    assert calls(log) == [
        ["checkpoint", "rm", "web", "cp1"],
        ["checkpoint", "rm", "--checkpoint-dir", str(tmp_path / "cp"), "web", "cp1"],
    ]


@pytest.mark.parametrize("operation", ["create", "list", "remove"])
def test_daemon_without_experimental_features_raises(
    tmp_path: Path, operation: str
) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("checkpoint",), stderr=EXPERIMENTAL, exit_code=1)]
    )
    cli = checkpoints(client)
    with pytest.raises(DockerException):
        match operation:
            case "create":
                cli.create("web", "cp1")
            case "list":
                cli.list("web")
            case _:
                cli.remove("web", "cp1")


def test_create_releases_the_pin_of_a_container_handle(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("--size",), stdout=SIZED_INSPECT),
            Rule(match=("inspect",), stdout=PLAIN_INSPECT),
        ],
    )
    with patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 0):
        container = client.container.inspect("web", size=True)
        assert container.size_rw == 1024

        checkpoints(client).create(container, "cp1", leave_running=True)
        assert container.size_rw is None
    assert calls(log)[1] == [
        "checkpoint",
        "create",
        "--leave-running",
        CONTAINER_ID,
        "cp1",
    ]


def test_failed_create_still_releases_the_pin(tmp_path: Path) -> None:
    client, _ = make_fake_cli(
        tmp_path,
        [
            Rule(match=("checkpoint",), stderr=EXPERIMENTAL, exit_code=1),
            Rule(match=("--size",), stdout=SIZED_INSPECT),
            Rule(match=("inspect",), stdout=PLAIN_INSPECT),
        ],
    )
    with patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 0):
        container = client.container.inspect("web", size=True)
        with pytest.raises(DockerException):
            checkpoints(client).create(container, "cp1")
        assert container.size_rw is None
