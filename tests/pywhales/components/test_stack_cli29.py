"""Argv tests for Docker CLI 29 stack config, deploy, remove, ps and services.

A recording fake CLI stands in for Docker, so nothing here touches a daemon or a
Swarm.
"""

import inspect
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest

from pywhales import Stack
from pywhales.components.service.cli_wrapper import ServiceListFilter
from pywhales.components.stack.cli_wrapper import StackCLI, StackPsFilter
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

MERGED_YAML = "services:\n  web:\n    image: busybox\n"


def test_config_returns_the_cli_text_for_one_file(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("config",), stdout=MERGED_YAML)])
    assert client.stack.config("a.yml") == MERGED_YAML.rstrip("\n")
    assert calls(log) == [["stack", "config", "--compose-file", "a.yml"]]


def test_config_repeats_the_compose_file_option_in_order(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.config([Path("base.yml"), "override.yml", "-"])
    assert calls(log) == [
        [
            "stack",
            "config",
            "--compose-file",
            "base.yml",
            "--compose-file",
            "override.yml",
            "--compose-file",
            "-",
        ]
    ]


def test_config_skip_interpolation_is_a_valueless_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.config("a.yml", skip_interpolation=True)
    client.stack.config("a.yml", skip_interpolation=False)
    assert calls(log) == [
        ["stack", "config", "--compose-file", "a.yml", "--skip-interpolation"],
        ["stack", "config", "--compose-file", "a.yml"],
    ]


def test_config_skip_interpolation_is_keyword_only() -> None:
    parameters = inspect.signature(StackCLI.config).parameters
    assert list(parameters)[1:] == ["compose_files", "skip_interpolation"]
    assert parameters["skip_interpolation"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["skip_interpolation"].default is False


@pytest.mark.parametrize(
    ("detach", "expected"),
    [(None, []), (True, ["--detach"]), (False, ["--detach=false"])],
)
def test_deploy_detach_is_tristate(
    tmp_path: Path, detach: bool | None, expected: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.deploy("web", "a.yml", detach=detach)
    assert calls(log) == [
        [
            "stack",
            "deploy",
            "--compose-file",
            "a.yml",
            "--resolve-image",
            "always",
            *expected,
            "web",
        ]
    ]


def test_deploy_quiet_is_a_valueless_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.deploy("web", "a.yml", quiet=True)
    client.stack.deploy("web", "a.yml", quiet=False)
    assert calls(log) == [
        [
            "stack",
            "deploy",
            "--compose-file",
            "a.yml",
            "--resolve-image",
            "always",
            "--quiet",
            "web",
        ],
        [
            "stack",
            "deploy",
            "--compose-file",
            "a.yml",
            "--resolve-image",
            "always",
            "web",
        ],
    ]


def test_deploy_forwards_every_option_together(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    stack = client.stack.deploy(
        "web",
        ["a.yml", "b.yml"],
        True,
        "never",
        True,
        detach=False,
        quiet=True,
    )
    assert stack == Stack(client.client_config, "web")
    assert calls(log) == [
        [
            "stack",
            "deploy",
            "--compose-file",
            "a.yml",
            "--compose-file",
            "b.yml",
            "--prune",
            "--resolve-image",
            "never",
            "--with-registry-auth",
            "--detach=false",
            "--quiet",
            "web",
        ]
    ]


def test_deploy_keeps_its_positionals_and_adds_keyword_only_options() -> None:
    parameters = inspect.signature(StackCLI.deploy).parameters
    assert list(parameters)[1:] == [
        "name",
        "compose_files",
        "prune",
        "resolve_image",
        "with_registry_auth",
        "env_files",
        "variables",
        "detach",
        "quiet",
        "timeout",
    ]
    for name in ("detach", "quiet", "timeout"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["detach"].default is None
    assert parameters["quiet"].default is False


@pytest.mark.parametrize(
    ("detach", "expected"),
    [(None, []), (True, ["--detach"]), (False, ["--detach=false"])],
)
def test_remove_detach_is_tristate(
    tmp_path: Path, detach: bool | None, expected: list[str]
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.remove(["alpha", "beta"], detach=detach)
    assert calls(log) == [["stack", "remove", *expected, "alpha", "beta"]]


def test_remove_empty_selection_launches_nothing_with_detach(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.remove([], detach=False)
    assert calls(log) == []


def test_remove_detach_is_keyword_only() -> None:
    parameters = inspect.signature(StackCLI.remove).parameters
    assert list(parameters)[1:] == ["x", "detach"]
    assert parameters["detach"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["detach"].default is None


def test_handle_remove_forwards_detach(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Stack(client.client_config, "alpha").remove(detach=False)
    Stack(client.client_config, "alpha").remove()
    assert calls(log) == [
        ["stack", "remove", "--detach=false", "alpha"],
        ["stack", "remove", "alpha"],
    ]


def test_ps_forwards_filter_pairs_before_the_stack(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ps",), stdout="t1\nt2\n")])
    tasks = client.stack.ps(
        "alpha",
        filters=[("desired-state", "running"), ("node", "n1"), ("node", "n2")],
    )
    assert [task.id for task in tasks] == ["t1", "t2"]
    assert calls(log) == [
        [
            "stack",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "desired-state=running",
            "--filter",
            "node=n1",
            "--filter",
            "node=n2",
            "alpha",
        ]
    ]


@pytest.mark.parametrize(
    "filters", [{"name": "alpha_web"}, MappingProxyType({"name": "alpha_web"})]
)
def test_ps_accepts_a_mapping_of_filters(
    tmp_path: Path, filters: dict[str, str] | MappingProxyType[str, str]
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.ps("alpha", filters=filters)
    assert calls(log) == [
        [
            "stack",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "name=alpha_web",
            "alpha",
        ]
    ]


def test_ps_reads_a_filter_generator_once(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    filters: list[StackPsFilter] = [("id", "a"), ("id", "b")]
    client.stack.ps("alpha", filters=(pair for pair in filters))
    assert calls(log) == [
        [
            "stack",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "id=a",
            "--filter",
            "id=b",
            "alpha",
        ]
    ]


def test_ps_without_filters_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.ps("alpha")
    client.stack.ps(Stack(client.client_config, "beta"), filters=None)
    client.stack.ps("gamma", filters=())
    assert calls(log) == [
        ["stack", "ps", "--quiet", "--no-trunc", "alpha"],
        ["stack", "ps", "--quiet", "--no-trunc", "beta"],
        ["stack", "ps", "--quiet", "--no-trunc", "gamma"],
    ]


def test_handle_ps_forwards_filters(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Stack(client.client_config, "alpha").ps(filters=[("name", "alpha_web")])
    assert calls(log) == [
        [
            "stack",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "name=alpha_web",
            "alpha",
        ]
    ]


def test_services_forward_filter_pairs_before_the_stack(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert (
        client.stack.services(
            "alpha", filters=[("mode", "replicated"), ("label", "tier=web")]
        )
        == []
    )
    assert calls(log) == [
        [
            "stack",
            "services",
            "--quiet",
            "--filter",
            "mode=replicated",
            "--filter",
            "label=tier=web",
            "alpha",
        ]
    ]


def test_services_accept_a_mapping_and_a_generator(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    pairs: list[ServiceListFilter] = [("name", "alpha_web"), ("name", "alpha_db")]
    client.stack.services("alpha", filters={"id": "x"})
    client.stack.services("alpha", filters=(pair for pair in pairs))
    assert calls(log) == [
        ["stack", "services", "--quiet", "--filter", "id=x", "alpha"],
        [
            "stack",
            "services",
            "--quiet",
            "--filter",
            "name=alpha_web",
            "--filter",
            "name=alpha_db",
            "alpha",
        ],
    ]


def test_services_without_filters_keep_their_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.stack.services("alpha")
    client.stack.services(Stack(client.client_config, "beta"), filters=None)
    assert calls(log) == [
        ["stack", "services", "--quiet", "alpha"],
        ["stack", "services", "--quiet", "beta"],
    ]


def test_handle_services_forward_filters(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Stack(client.client_config, "alpha").services(filters=[("mode", "global")])
    assert calls(log) == [
        ["stack", "services", "--quiet", "--filter", "mode=global", "alpha"]
    ]


@pytest.mark.parametrize("method", ["ps", "services"])
@pytest.mark.parametrize("bad", ["name=web", b"name=web", [("name",)], [("a", 1)]])
def test_listing_filters_are_validated_before_any_launch(
    tmp_path: Path, method: str, bad: object
) -> None:
    client, log = make_fake_cli(tmp_path)
    listing: Callable[..., object] = getattr(client.stack, method)
    with pytest.raises(TypeError):
        listing("alpha", filters=bad)
    assert calls(log) == []


@pytest.mark.parametrize("method", [StackCLI.ps, StackCLI.services])
def test_listing_filters_are_keyword_only_with_a_none_default(
    method: Callable[..., object],
) -> None:
    parameter = inspect.signature(method).parameters["filters"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
