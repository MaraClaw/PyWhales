"""Argv tests for Docker CLI 29 `config inspect --pretty` and `secret inspect --pretty`.

A recording fake CLI stands in for Docker, so nothing here touches a daemon or a
Swarm.
"""

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from pywhales import Config, DockerClient, Secret
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

PRETTY_TEXT = "ID:\t\t\tabc\nName:\t\t\tapp\n"
KINDS = ["config", "secret"]


def _inspect(client: DockerClient, kind: str) -> Callable[..., object]:
    return client.config.inspect if kind == "config" else client.secret.inspect


@pytest.mark.parametrize("kind", KINDS)
def test_pretty_returns_the_cli_text_for_all_references(
    tmp_path: Path, kind: str
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("--pretty",), stdout=PRETTY_TEXT)]
    )
    assert _inspect(client, kind)(["app", "other"], pretty=True) == PRETTY_TEXT.rstrip(
        "\n"
    )
    assert calls(log) == [[kind, "inspect", "--pretty", "app", "other"]]


@pytest.mark.parametrize("kind", KINDS)
def test_pretty_accepts_a_single_reference(tmp_path: Path, kind: str) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    assert _inspect(client, kind)("app", pretty=True) == "text"
    assert calls(log) == [[kind, "inspect", "--pretty", "app"]]


def test_config_pretty_of_an_empty_list_launches_nothing(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.config.inspect([], pretty=True) == ""
    assert calls(log) == []


@pytest.mark.parametrize(
    "make_empty",
    [list, tuple, lambda: (item for item in ())],
    ids=["list", "tuple", "generator"],
)
def test_secret_pretty_of_empty_input_launches_nothing(
    tmp_path: Path, make_empty: Callable[[], Iterable[str]]
) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.secret.inspect(make_empty(), pretty=True) == ""
    assert calls(log) == []


def test_secret_pretty_reads_a_generator_of_references_once(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    references = (name for name in ["a", "b"])
    assert client.secret.inspect(references, pretty=True) == "text"
    assert calls(log) == [["secret", "inspect", "--pretty", "a", "b"]]


def test_config_pretty_never_pins_or_seeds_a_snapshot(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    handle = Config(client.client_config, "cfg-id", is_immutable_id=True)
    client.config.inspect(["cfg-id"], pretty=True)
    assert handle._inspect_result is None


def test_secret_pretty_never_pins_or_seeds_a_snapshot(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    handle = Secret(client.client_config, "sec-id", is_immutable_id=True)
    client.secret.inspect(["sec-id"], pretty=True)
    assert handle._inspect_result is None


@pytest.mark.parametrize("kind", KINDS)
def test_default_inspect_still_parses_json_without_a_pretty_flag(
    tmp_path: Path, kind: str
) -> None:
    identity = "x" * 25
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("inspect",), stdout=json.dumps([{"ID": identity}]))],
    )
    handle = _inspect(client, kind)(identity)
    assert isinstance(handle, Config | Secret)
    assert handle.id == identity
    assert calls(log) == [[kind, "inspect", identity]]
