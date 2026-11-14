"""Docker CLI 29.9.3 plugin commands asserted as exact argv; never launches Docker."""

from collections.abc import Callable
from pathlib import Path

import pytest

from pywhales import DockerClient, Plugin
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

FIRST_ID = "a" * 64
SECOND_ID = "b" * 64
INSPECT_RULE = Rule(
    match=("inspect",),
    stdout=f'[{{"Id": "{FIRST_ID}", "Name": "plug", "Enabled": true}}]',
)
LIST_RULE = Rule(match=("list",), stdout=f"{FIRST_ID}\n{SECOND_ID}\n")
LIST_ARGV = ["plugin", "list", "--no-trunc", "--quiet"]
FILTER_ARGV = ["--filter", "enabled=true", "--filter", "capability=volumedriver"]


def _handle(client: DockerClient) -> Plugin:
    return Plugin(client.client_config, "plug", is_immutable_id=True)


def _untyped_list(client: DockerClient) -> Callable[..., object]:
    """Return `plugin.list` without its signature, to pass invalid filters."""
    return client.plugin.list


def test_list_forwards_pair_filters_as_repeated_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [LIST_RULE])

    plugins = client.plugin.list([("enabled", "true"), ("capability", "volumedriver")])

    assert [plugin.id for plugin in plugins] == [FIRST_ID, SECOND_ID]
    assert calls(log) == [[*LIST_ARGV, *FILTER_ARGV]]


def test_list_forwards_a_filter_mapping(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [LIST_RULE])

    client.plugin.list({"enabled": "true", "capability": "volumedriver"})

    assert calls(log) == [[*LIST_ARGV, *FILTER_ARGV]]


@pytest.mark.parametrize("filters", [None, ()], ids=["none", "empty"])
def test_list_without_filters_keeps_its_argv(
    tmp_path: Path, filters: tuple[()] | None
) -> None:
    client, log = make_fake_cli(tmp_path, [LIST_RULE])

    client.plugin.list(filters)
    client.plugin.list()

    assert calls(log) == [LIST_ARGV, LIST_ARGV]


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param("enabled=true", id="string"),
        pytest.param([("enabled",)], id="short-pair"),
        pytest.param([("enabled", 1)], id="non-string-value"),
        pytest.param(7, id="not-iterable"),
    ],
)
def test_list_rejects_malformed_filters_before_launch(
    tmp_path: Path, filters: object
) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(TypeError):
        _untyped_list(client)(filters)

    assert calls(log) == []


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        pytest.param(
            lambda c: c.plugin.install("plug", {"k": "v"}, "alias", True, True),
            [
                "plugin",
                "install",
                "--grant-all-permissions",
                "--alias",
                "alias",
                "--disable",
                "plug",
                "k=v",
            ],
            id="install",
        ),
        pytest.param(
            lambda c: c.plugin.push("plug"), ["plugin", "push", "plug"], id="push"
        ),
        pytest.param(
            lambda c: _handle(c).push(True),
            ["plugin", "push", "plug"],
            id="handle-push",
        ),
        pytest.param(
            lambda c: c.plugin.upgrade("plug", "remote:tag", True, True),
            [
                "plugin",
                "upgrade",
                "--grant-all-permissions",
                "--skip-remote-check",
                "plug",
                "remote:tag",
            ],
            id="upgrade",
        ),
        pytest.param(
            lambda c: _handle(c).upgrade("remote:tag", True),
            ["plugin", "upgrade", "--grant-all-permissions", "plug", "remote:tag"],
            id="handle-upgrade",
        ),
    ],
)
def test_default_content_trust_policy_emits_no_trust_option(
    tmp_path: Path,
    operation: Callable[[DockerClient], object],
    expected: list[str],
) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    operation(client)

    assert calls(log)[0] == expected
    assert not any("content-trust" in token for call in calls(log) for token in call)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda c: c.plugin.install("plug", disable_content_trust=False),
            id="install",
        ),
        pytest.param(
            lambda c: c.plugin.push("plug", disable_content_trust=False), id="push"
        ),
        pytest.param(
            lambda c: _handle(c).push(disable_content_trust=False), id="handle-push"
        ),
        pytest.param(
            lambda c: c.plugin.upgrade("plug", disable_content_trust=False),
            id="upgrade",
        ),
        pytest.param(
            lambda c: _handle(c).upgrade(disable_content_trust=False),
            id="handle-upgrade",
        ),
    ],
)
def test_enabling_content_trust_is_rejected_before_launch(
    tmp_path: Path, operation: Callable[[DockerClient], object]
) -> None:
    client, log = make_fake_cli(tmp_path, [INSPECT_RULE])

    with pytest.raises(ValueError):
        operation(client)

    assert calls(log) == []
