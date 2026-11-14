from pathlib import Path

import pytest

import pywhales.components.buildx.imagetools.cli_wrapper as imagetools
from pywhales import DockerClient
from pywhales.client_config import Command
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

INDEX = '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json"}'


def test_create_accepts_default_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[Command] = []

    def run(command: Command) -> str:
        commands.append(command)
        return (
            '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json"}'
        )

    monkeypatch.setattr(imagetools, "run", run)

    manifest = DockerClient().buildx.imagetools.create(["example:latest"], dry_run=True)

    assert manifest.schema_version == 2
    assert manifest.media_type == "application/vnd.oci.image.index.v1+json"
    assert len(commands) == 1
    assert commands[0][-1] == "example:latest"
    assert "--dry-run" in commands[0]
    assert "--tag" not in commands[0]
    assert "--annotation" not in commands[0]
    assert "--file" not in commands[0]


def test_create_adds_no_new_option_by_default(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout=INDEX)])
    client.buildx.imagetools.create(["alpine"], ["example:latest"], dry_run=True)
    assert calls(log) == [
        [
            "buildx",
            "imagetools",
            "create",
            "--tag",
            "example:latest",
            "--dry-run",
            "alpine",
        ]
    ]


def test_create_forwards_the_modern_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("create",), stdout=INDEX)])
    metadata = tmp_path / "metadata.json"
    manifest = client.buildx.imagetools.create(
        ["alpine"],
        ["example:latest"],
        dry_run=True,
        builder="b1",
        prefer_index=False,
        metadata_file=metadata,
        platforms=["linux/amd64", "linux/arm64"],
        progress="plain",
        debug=True,
    )
    client.buildx.imagetools.create(["alpine"], prefer_index=True)
    client.buildx.imagetools.create(["alpine"], prefer_index=None)
    assert manifest.schema_version == 2
    assert calls(log) == [
        [
            "buildx",
            "imagetools",
            "create",
            "--debug",
            "--tag",
            "example:latest",
            "--builder",
            "b1",
            "--dry-run",
            "--prefer-index=false",
            "--metadata-file",
            str(metadata),
            "--platform",
            "linux/amd64",
            "--platform",
            "linux/arm64",
            "--progress",
            "plain",
            "alpine",
        ],
        ["buildx", "imagetools", "create", "--prefer-index", "alpine"],
        ["buildx", "imagetools", "create", "alpine"],
    ]


def test_inspect_keeps_the_raw_manifest_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("inspect",), stdout=INDEX)])
    manifest = client.buildx.imagetools.inspect("alpine")
    client.buildx.imagetools.inspect("alpine", builder="b1")
    assert manifest.media_type == "application/vnd.oci.image.index.v1+json"
    assert calls(log) == [
        ["buildx", "imagetools", "inspect", "--raw", "alpine"],
        ["buildx", "imagetools", "inspect", "--builder", "b1", "--raw", "alpine"],
    ]


def test_inspect_debug_returns_human_text_and_the_debug_log(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("inspect",), stdout="Name: alpine\n", stderr="debug log\n")],
    )
    text = client.buildx.imagetools.inspect("alpine", builder="b1", debug=True)
    assert text == "Name: alpine\ndebug log"
    assert calls(log) == [
        [
            "buildx",
            "imagetools",
            "inspect",
            "--debug",
            "--builder",
            "b1",
            "alpine",
        ]
    ]


def test_create_forwards_annotations_files_and_append(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.imagetools.create(
        ["alpine"],
        ["example:latest"],
        append=True,
        annotations={"index:org.example.k": "v"},
        files=[tmp_path / "source.json"],
    )
    assert calls(log) == [
        [
            "buildx",
            "imagetools",
            "create",
            "--tag",
            "example:latest",
            "--annotation",
            "index:org.example.k=v",
            "--file",
            str(tmp_path / "source.json"),
            "--append",
            "alpine",
        ]
    ]


def test_create_rejects_a_bare_string_sequence_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    imagetools = client.buildx.imagetools
    with pytest.raises(TypeError):
        imagetools.create("alpine")
    with pytest.raises(TypeError):
        imagetools.create(["alpine"], tags="example:latest")
    with pytest.raises(TypeError):
        imagetools.create(["alpine"], files="source.json")
    with pytest.raises(TypeError):
        imagetools.create(["alpine"], platforms="linux/arm64")
    assert calls(log) == []
