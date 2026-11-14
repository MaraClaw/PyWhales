"""Argv tests for Docker CLI 29 node inspect, list and ps options.

Recording or in-memory fake CLIs stand in for Docker, so nothing here touches a
daemon or a Swarm.
"""

import inspect
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest

from pywhales import DockerClient, Node
from pywhales.components.node.cli_wrapper import NodeCLI, NodeListFilter
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli
from tests.pywhales.components.hydration_fakes import FakeCli, fake_daemon, identity_of

PRETTY_TEXT = "ID:\t\tabc\nHostname:\thost\n"


def _operation(client: DockerClient, name: str) -> Callable[..., object]:
    """Return a node operation addressed by name, so malformed input type-checks."""
    return getattr(client.node, name)


def test_inspect_pretty_returns_the_cli_text_without_creating_handles(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("--pretty",), stdout=PRETTY_TEXT)]
    )
    text = client.node.inspect(["self", "worker-1"], pretty=True)
    assert text == PRETTY_TEXT.rstrip("\n")
    assert calls(log) == [["node", "inspect", "--pretty", "self", "worker-1"]]


def test_inspect_pretty_accepts_a_single_reference(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    assert client.node.inspect("self", pretty=True) == "text"
    assert calls(log) == [["node", "inspect", "--pretty", "self"]]


def test_inspect_pretty_never_pins_or_seeds_a_snapshot(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("--pretty",), stdout="text")])
    handle = Node(client.client_config, "node-id", is_immutable_id=True)
    client.node.inspect(["node-id"], pretty=True)
    assert handle._inspect_result is None
    assert [argv[:2] for argv in calls(log)] == [["node", "inspect"]]


def test_inspect_pretty_of_an_empty_list_launches_nothing(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.node.inspect([], pretty=True) == ""
    assert calls(log) == []


def test_default_inspect_has_no_pretty_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("inspect",), stdout='[{"ID": "n1"}]')]
    )
    client.node.inspect("n1")
    assert calls(log) == [["node", "inspect", "n1"]]


def test_list_forwards_filter_pairs_in_order_with_repeats(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("list",), stdout="a\nb\n")])
    nodes = client.node.list(
        [("role", "manager"), ("label", "x=1"), ("label", "y=2"), ("node.label", "r")]
    )
    assert [node.id for node in nodes] == ["a", "b"]
    assert calls(log) == [
        [
            "node",
            "list",
            "--quiet",
            "--filter",
            "role=manager",
            "--filter",
            "label=x=1",
            "--filter",
            "label=y=2",
            "--filter",
            "node.label=r",
        ]
    ]


@pytest.mark.parametrize(
    "filters", [{"role": "manager"}, MappingProxyType({"role": "manager"})]
)
def test_list_accepts_a_mapping_of_filters(
    tmp_path: Path, filters: dict[str, str] | MappingProxyType[str, str]
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.node.list(filters)
    assert calls(log) == [["node", "list", "--quiet", "--filter", "role=manager"]]


def test_list_reads_a_filter_generator_once(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    filters: list[NodeListFilter] = [("name", "a"), ("name", "b")]
    client.node.list(pair for pair in filters)
    assert calls(log) == [
        ["node", "list", "--quiet", "--filter", "name=a", "--filter", "name=b"]
    ]


def test_list_without_filters_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.node.list()
    client.node.list(())
    assert calls(log) == [["node", "list", "--quiet"]] * 2


def test_list_filters_are_the_only_new_positional() -> None:
    parameters = inspect.signature(NodeCLI.list).parameters
    assert list(parameters)[1:] == ["filters", "hydrate"]
    assert parameters["filters"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["hydrate"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("bad", ["role=manager", b"role=manager", [("role",)]])
def test_list_rejects_malformed_filters_before_any_launch(
    tmp_path: Path, bad: object
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(TypeError):
        _operation(client, "list")(bad)
    assert calls(log) == []


def test_hydrated_list_forwards_filters_to_its_listing() -> None:
    ids = [identity_of("node", index) for index in range(3)]
    cli = FakeCli("node", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        nodes = client.node.list([("role", "worker"), ("name", "host")], hydrate=True)
        assert [node.id for node in nodes] == ids
    assert cli.lists() == [
        [
            "docker-test",
            "node",
            "list",
            "--quiet",
            "--filter",
            "role=worker",
            "--filter",
            "name=host",
        ]
    ]
    assert cli.inspects() == [ids[:2], ids[2:]]


def test_hydrated_list_without_filters_keeps_its_listing_argv() -> None:
    ids = [identity_of("node", index) for index in range(2)]
    cli = FakeCli("node", ids)
    with fake_daemon(cli) as client:
        client.node.list(hydrate=True)
    assert cli.lists() == [["docker-test", "node", "list", "--quiet"]]


def test_hydrated_list_still_drops_vanished_nodes_with_filters() -> None:
    ids = [identity_of("node", index) for index in range(3)]
    cli = FakeCli("node", ids)
    cli.vanish_on_list = {ids[1]}
    with fake_daemon(cli) as client:
        nodes = client.node.list([("role", "worker")], hydrate=True)
        assert [node.id for node in nodes] == [ids[0], ids[2]]
    assert len(cli.lists()) == 1
    assert cli.inspects() == [ids, ids[:1], ids[1:], ids[1:2], ids[2:]]


def test_ps_forwards_filters_before_the_nodes(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ps",), stdout="t1\nt2\n")])
    tasks = client.node.ps(
        ["node-a", "node-b"],
        filters=[("desired-state", "running"), ("name", "web")],
    )
    assert [task.id for task in tasks] == ["t1", "t2"]
    assert calls(log) == [
        [
            "node",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "desired-state=running",
            "--filter",
            "name=web",
            "node-a",
            "node-b",
        ]
    ]


def test_ps_filters_apply_to_the_current_node_default(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.node.ps(filters={"label": "usage"})
    assert calls(log) == [
        ["node", "ps", "--quiet", "--no-trunc", "--filter", "label=usage"]
    ]


def test_ps_without_filters_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.node.ps("alpha")
    client.node.ps("alpha", filters=None)
    client.node.ps("alpha", filters=())
    assert calls(log) == [["node", "ps", "--quiet", "--no-trunc", "alpha"]] * 3


def test_ps_filters_are_keyword_only() -> None:
    parameter = inspect.signature(NodeCLI.ps).parameters["filters"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_ps_rejects_malformed_filters_before_any_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    list_tasks = _operation(client, "ps")
    with pytest.raises(TypeError):
        list_tasks("alpha", filters="desired-state=running")
    with pytest.raises(TypeError):
        list_tasks([], filters=[("only-key",)])
    assert calls(log) == []


def test_ps_empty_selection_still_launches_nothing_with_filters(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.node.ps([], filters=[("name", "web")]) == []
    assert calls(log) == []


def test_handle_ps_forwards_filters(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ps",), stdout="t1\n")])
    node = Node(client.client_config, "node-id", is_immutable_id=True)
    assert [task.id for task in node.ps(filters=[("desired-state", "shutdown")])] == [
        "t1"
    ]
    assert calls(log) == [
        [
            "node",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "desired-state=shutdown",
            "node-id",
        ]
    ]
