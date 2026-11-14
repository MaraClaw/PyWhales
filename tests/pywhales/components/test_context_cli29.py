"""Docker CLI 29.9.3 context commands asserted as exact argv; never launches Docker."""

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from pywhales import (
    Context,
    DockerClient,
    DockerContextConfig,
    KubernetesContextConfig,
)
from pywhales.exceptions import DockerTimeout
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

ENDPOINT = "host=unix:///var/run/docker.sock,skip-tls-verify=False"
INSPECT_RULE = Rule(
    match=("inspect",), stdout='[{"Name": "ctx", "Metadata": {"Description": "x"}}]'
)


def test_show_returns_the_current_context_name(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("show",), stdout="remote\n")])

    assert client.context.show() == "remote"
    assert calls(log) == [["context", "show"]]


@pytest.mark.parametrize("as_handle", [False, True], ids=["name", "handle"])
@pytest.mark.parametrize("output", [None, "ctx.tar", Path("out/ctx.tar")])
def test_export_names_the_archive_only_when_given(
    tmp_path: Path, as_handle: bool, output: str | Path | None
) -> None:
    client, log = make_fake_cli(tmp_path)
    context = Context(client.client_config, "ctx", is_immutable_id=True)

    assert client.context.export(context if as_handle else "ctx", output) is None

    expected = ["context", "export", "ctx"]
    assert calls(log) == [expected if output is None else [*expected, str(output)]]


def test_import_inspects_the_imported_context(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])
    archive = tmp_path / "ctx.tar"

    imported = client.context.import_("ctx", archive)

    assert isinstance(imported, Context)
    assert imported.client_config is client.client_config
    assert calls(log) == [
        ["context", "import", "ctx", str(archive)],
        ["context", "inspect", "ctx"],
    ]
    assert imported.name == "ctx"


@pytest.mark.parametrize("as_handle", [False, True], ids=["name", "handle"])
@pytest.mark.parametrize("as_dict", [False, True], ids=["dataclass", "dict"])
def test_update_forwards_description_and_the_create_endpoint_encoding(
    tmp_path: Path, as_handle: bool, as_dict: bool
) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])
    context = Context(client.client_config, "ctx", is_immutable_id=True)
    host = "unix:///var/run/docker.sock"

    updated = client.context.update(
        context if as_handle else "ctx",
        description="x",
        docker={"host": host} if as_dict else DockerContextConfig(host=host),
    )

    assert isinstance(updated, Context)
    assert calls(log) == [
        ["context", "update", "--description", "x", "--docker", ENDPOINT, "ctx"],
        ["context", "inspect", "ctx"],
    ]
    assert updated.name == "ctx"


def test_update_omits_unspecified_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    client.context.update("ctx")

    assert calls(log) == [["context", "update", "ctx"], ["context", "inspect", "ctx"]]


def test_handle_update_reloads_the_snapshot(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])
    context = Context(client.client_config, "ctx", is_immutable_id=True)

    assert context.update(description="x") is None

    assert calls(log) == [
        ["context", "update", "--description", "x", "ctx"],
        ["context", "inspect", "ctx"],
        ["context", "inspect", "ctx"],
    ]
    assert context.metadata == {"Description": "x"}


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, True])
def test_archive_commands_reject_an_invalid_timeout_before_launch(
    tmp_path: Path, timeout: float
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError):
        client.context.export("ctx", timeout=timeout)
    with pytest.raises(ValueError):
        client.context.import_("ctx", "ctx.tar", timeout=timeout)

    assert calls(log) == []


@pytest.mark.parametrize("stream", ["-", Path("-")])
def test_archive_commands_reject_standard_streams_before_launch(
    tmp_path: Path, stream: str | Path
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError):
        client.context.export("ctx", stream)
    with pytest.raises(ValueError):
        client.context.import_("ctx", stream)

    assert calls(log) == []


@pytest.mark.parametrize("command", ["export", "import"])
def test_archive_timeout_is_a_local_deadline(tmp_path: Path, command: str) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=(command,), sleep=30)])

    with pytest.raises(DockerTimeout):
        if command == "export":
            client.context.export("ctx", "ctx.tar", timeout=0.3)
        else:
            client.context.import_("ctx", "ctx.tar", timeout=0.3)

    assert calls(log) == [["context", command, "ctx", "ctx.tar"]]


@pytest.mark.parametrize(
    "create",
    [
        pytest.param(lambda c: c.context.create("ctx", "swarm"), id="positional"),
        pytest.param(
            lambda c: c.context.create("ctx", default_stack_orchestrator="swarm"),
            id="orchestrator",
        ),
        pytest.param(lambda c: c.context.create("ctx", kubernetes={}), id="empty-dict"),
        pytest.param(
            lambda c: c.context.create(
                "ctx", kubernetes={"config_file": "/kube/config"}
            ),
            id="dict",
        ),
        pytest.param(
            lambda c: c.context.create("ctx", kubernetes=KubernetesContextConfig()),
            id="dataclass",
        ),
    ],
)
def test_create_rejects_removed_parameters_before_launch(
    tmp_path: Path, create: Callable[[DockerClient], object]
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError) as raised:
        create(client)

    assert "swarm" not in str(raised.value)
    assert "/kube/config" not in str(raised.value)
    assert calls(log) == []
