import copy
import inspect
import json
import pickle
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Node
from pywhales.client_config import ParsingError
from pywhales.components.node.models import NodeInspectResult
from pywhales.exceptions import DockerException
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


def test_batch_inspect_matches_node_hostnames_and_hydrates_cache() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    alpha = {
        "ID": "a" * 27,
        "Spec": {"Name": "not-alpha"},
        "Description": {"Hostname": "alpha"},
    }
    bravo = {
        "ID": "b" * 27,
        "Spec": {"Name": "not-bravo"},
        "Description": {"Hostname": "bravo"},
    }
    references = ["bravo", "a" * 27, "b" * 8, "alpha", "bravo"]
    response = json.dumps([alpha, bravo, alpha, bravo, bravo]).encode()
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 0, response, b"")
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = client.node.inspect(references)
        assert [resource.id for resource in resources] == [
            "b" * 27,
            "a" * 27,
            "b" * 27,
            "a" * 27,
            "b" * 27,
        ]
        assert [
            resource.description.hostname
            for resource in resources
            if resource.description is not None
        ] == ["bravo", "alpha", "bravo", "alpha", "bravo"]
        assert all(
            isinstance(resource._get_inspect_result(), NodeInspectResult)
            for resource in resources
        )
        assert all(
            resource.client_config is client.client_config for resource in resources
        )
        assert resources[0] is not resources[4]
        command.assert_called_once()
        assert command.call_args.args[0] == [
            "docker-test",
            "--host",
            "inventory-host",
            "node",
            "inspect",
            *references,
        ]


def test_batch_inspect_node_scalar_and_lazy_list_contracts() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess(
            [], 0, b'[{"ID":"identity","Description":{"Hostname":"named"}}]', b""
        ),
    ) as command:
        resource = client.node.inspect("named")
        assert isinstance(resource, Node)
        assert resource.id == "identity"
        assert isinstance(resource._inspect_result, NodeInspectResult)
        command.return_value = CompletedProcess([], 0, b"identity", b"")
        listed = client.node.list()
        assert listed[0].id == "identity"
        assert listed[0]._inspect_result is None
        assert command.call_count == 2


def test_batch_inspect_empty_node_avoids_executable_lookup() -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        assert client.node.inspect([]) == []


@pytest.mark.parametrize(
    "payload",
    [
        "invalid json",
        "{}",
        "[]",
        "[null,null]",
        "[{},{}]",
        '[{"ID":"first"}]',
        '[{"ID":"first"},{"ID":"second"},{}]',
        '[{"ID":"wrong"},{"ID":"second"}]',
        '[{"ID":""},{"ID":"second"}]',
        '[{"ID":"first"},{"ID":"second","CreatedAt":"invalid"}]',
        '[{"ID":"one","Description":{"Hostname":"first"}},{"ID":"two","Description":{"Hostname":"first"}}]',
        '[{"ID":"one","Spec":{"Name":"first"}},{"ID":"two","Spec":{"Name":"second"}}]',
    ],
)
def test_batch_inspect_node_rejects_invalid_response(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        patch.object(Node, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.node.inspect(["first", "second"])
    hydrate.assert_not_called()


def test_batch_inspect_node_preserves_partial_command_failure() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    output = b'[{"ID":"first"}]'
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 17, output, b"permission denied"),
        ) as command,
        patch.object(Node, "_set_inspect_result") as hydrate,
        pytest.raises(DockerException) as failure,
    ):
        client.node.inspect(["first", "second"])
    assert failure.value.return_code == 17
    assert failure.value.stdout == output.decode()
    assert failure.value.stderr == "permission denied"
    assert failure.value.docker_command == [
        "docker-test",
        "node",
        "inspect",
        "first",
        "second",
    ]
    command.assert_called_once()
    hydrate.assert_not_called()


@pytest.mark.parametrize("json_file", get_all_jsons("nodes"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    a: NodeInspectResult = NodeInspectResult(**json.loads(json_as_txt))
    if json_file.name == "1.json":
        assert a.description is not None
        assert a.description.resources is not None
        generic_resources = a.description.resources.generic_resources
        assert generic_resources is not None
        named_resource_spec = generic_resources[0].named_resource_spec
        assert named_resource_spec is not None
        assert named_resource_spec.kind == "gpu"
        assert named_resource_spec.value == "gpu-0"
        assert a.description.resources.nano_cpus == 4000000001


@pytest.mark.usefixtures("swarm_mode")
def test_list_nodes(docker_client: DockerClient) -> None:
    nodes = docker_client.node.list()
    assert nodes[0].id[:12] in repr(nodes)
    assert len(nodes) == 1


@pytest.mark.usefixtures("swarm_mode")
def test_add_label(docker_client: DockerClient) -> None:
    nodes = docker_client.node.list()
    nodes[0].update(labels_add={"foo": "bar"})
    assert nodes[0].spec is not None
    labels = nodes[0].spec.labels
    assert labels is not None
    assert labels["foo"] == "bar"


@pytest.mark.usefixtures("swarm_mode")
def test_remove_label(docker_client: DockerClient) -> None:
    nodes = docker_client.node.list()
    nodes[0].update(labels_add={"foo": "bar"})
    nodes[0].update(rm_labels=["foo"])
    assert nodes[0].spec is not None
    labels = nodes[0].spec.labels
    assert labels is not None
    assert "foo" not in labels


@pytest.mark.usefixtures("swarm_mode")
def test_tasks(docker_client: DockerClient) -> None:
    service = docker_client.service.create("busybox", ["sleep", "infinity"])

    current_node = docker_client.node.list()[0]
    tasks = current_node.ps()
    assert len(tasks) > 0
    assert tasks[0].desired_state == "running"
    docker_client.service.remove(service)


@pytest.mark.usefixtures("swarm_mode")
def test_list_tasks_node(docker_client: DockerClient) -> None:
    with docker_client.service.create("busybox", ["sleep", "infinity"]) as my_service:
        assert docker_client.node.ps([]) == []
        assert set(docker_client.node.ps()) == set(docker_client.service.ps(my_service))


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
@pytest.mark.parametrize("method", ["demote", "promote", "remove", "ps"])
def test_node_empty_selection_skips_cli(
    method: str, make_empty: Callable[[], Iterable[str]]
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        result = getattr(client.node, method)(make_empty())
    assert result == ([] if method == "ps" else None)


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
@pytest.mark.parametrize(
    ("method", "kwargs", "subcommand"),
    [
        ("demote", {}, ["demote"]),
        ("promote", {}, ["promote"]),
        ("remove", {}, ["remove"]),
        ("remove", {"force": True}, ["remove", "--force"]),
    ],
)
def test_node_selection_builds_argv_once(
    method: str,
    kwargs: dict[str, bool],
    subcommand: list[str],
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        getattr(client.node, method)(make_selection(), **kwargs)
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "node", *subcommand, "alpha"]


@pytest.mark.parametrize(
    ("make_selection", "selected"),
    [
        pytest.param(lambda: None, [], id="current-node"),
        pytest.param(lambda: "alpha", ["alpha"], id="str"),
        pytest.param(lambda: ["alpha"], ["alpha"], id="list"),
        pytest.param(lambda: (item for item in ["alpha"]), ["alpha"], id="generator"),
    ],
)
def test_node_ps_launches_for_current_node_or_nonempty_selection(
    make_selection: Callable[[], str | Iterable[str] | None], selected: list[str]
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"task-id\n", b"")
    ) as command:
        tasks = client.node.ps(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "node",
        "ps",
        "--quiet",
        "--no-trunc",
        *selected,
    ]
    assert [task.id for task in tasks] == ["task-id"]


def _node_ids(count: int) -> list[str]:
    return [identity_of("node", index) for index in range(count)]


def test_hydrated_node_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _node_ids(5)
    cli = FakeCli("node", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        nodes = client.node.list(hydrate=True)
        launches = len(cli.launches)
        assert [node.id for node in nodes] == ids
        for node in nodes:
            assert node._inspect_result is not None
            assert node._inspect_result.id == node.id
        assert [
            node.description.hostname for node in nodes if node.description is not None
        ] == [f"host-{identity[:7]}" for identity in ids]
        assert all(node.status is not None for node in nodes)
    assert launches == 4
    assert cli.lists() == [["docker-test", "node", "list", "--quiet"]]
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]
    assert len(cli.launches) == launches


@pytest.mark.parametrize(
    ("count", "chunks"), [(0, []), (100, [100]), (101, [100, 1])], ids=str
)
def test_hydrated_node_list_inspects_at_most_one_chunk_bound_per_launch(
    count: int, chunks: list[int]
) -> None:
    ids = _node_ids(count)
    cli = FakeCli("node", ids)
    with fake_daemon(cli) as client:
        nodes = client.node.list(hydrate=True)
    assert [node.id for node in nodes] == ids
    assert [len(batch) for batch in cli.inspects()] == chunks


def test_default_node_list_stays_lazy_and_expiring() -> None:
    ids = _node_ids(3)
    cli = FakeCli("node", ids)
    with fake_daemon(cli) as client:
        nodes = client.node.list()
        assert [node.id for node in nodes] == ids
        assert cli.inspects() == []
        assert nodes[0].status is not None
        assert nodes[0].status is not None
        hydrate = inspect.signature(client.node.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.lists() == [["docker-test", "node", "list", "--quiet"]]
    assert cli.inspects() == [[ids[0]], [ids[0]]]
    assert len(cli.launches) == 3


def test_vanished_nodes_reprobe_only_the_affected_chunk() -> None:
    ids = _node_ids(6)
    cli = FakeCli("node", ids)
    cli.vanish_on_list = {ids[1], ids[2]}
    with fake_daemon(cli, chunk_size=3) as client:
        nodes = client.node.list(hydrate=True)
        launches = len(cli.launches)
        assert [node.id for node in nodes] == [ids[0], *ids[3:]]
        assert all(node.status is not None for node in nodes)
    assert len(cli.launches) == launches
    assert cli.inspects() == [
        ids[:3],
        ids[:1],
        ids[1:3],
        ids[1:2],
        ids[2:3],
        ids[3:],
    ]


def test_hydrated_node_list_is_empty_when_every_node_vanished() -> None:
    ids = _node_ids(3)
    cli = FakeCli("node", ids)
    cli.vanish_on_list = set(ids)
    with fake_daemon(cli) as client:
        assert client.node.list(hydrate=True) == []
    assert [len(batch) for batch in cli.inspects()] == [3, 1, 2, 1, 1]


@pytest.mark.parametrize("failure", list(INSPECT_FAILURES))
def test_hydrated_node_list_does_not_hide_other_inspect_failures(failure: str) -> None:
    ids = _node_ids(4)
    cli = FakeCli("node", ids)
    expected = INSPECT_FAILURES[failure]
    with (
        fake_daemon(cli) as client,
        patch("subprocess.run", side_effect=cli.failing({"inspect"}, failure)),
        pytest.raises(expected) as caught,
    ):
        client.node.list(hydrate=True)
    assert type(caught.value) is expected
    assert len(cli.inspects()) == 1


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            lambda ids: "Error response from daemon: node unrequested not found",
            id="unrequested-node",
        ),
        pytest.param(
            lambda ids: (
                f"Error response from daemon: node {ids[0]} not found\n"
                "Error response from daemon: boom"
            ),
            id="mixed-with-another-failure",
        ),
        pytest.param(lambda ids: f"Error: No such node: {ids[0]}", id="other-phrase"),
    ],
)
def test_hydrated_node_list_bisects_only_exact_missing_node_responses(
    stderr: Callable[[list[str]], str],
) -> None:
    ids = _node_ids(4)
    cli = FakeCli("node", ids)
    with (
        fake_daemon(cli) as client,
        patch(
            "subprocess.run",
            side_effect=cli.failing({"inspect"}, stderr(ids).encode()),
        ),
        pytest.raises(DockerException) as caught,
    ):
        client.node.list(hydrate=True)
    assert type(caught.value) is DockerException
    assert len(cli.inspects()) == 1


def test_hydrated_node_list_matches_results_by_exact_identity() -> None:
    ids = _node_ids(3)
    cli = FakeCli("node", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for node in client.node.list(hydrate=True):
            assert node._inspect_result is not None
            assert node._inspect_result.id == node.id
        cli.corrupt = {ids[1]: identity_of("node", 99)}
        with pytest.raises(ParsingError):
            client.node.list(hydrate=True)


_NODE_MUTATIONS: dict[str, Callable[[DockerClient, Node], object]] = {
    "handle-update": lambda client, node: node.update(availability="drain"),
    "cli-update": lambda client, node: client.node.update(node, role="worker"),
    "promote": lambda client, node: client.node.promote(node),
    "promote-list": lambda client, node: client.node.promote([node]),
    "demote": lambda client, node: client.node.demote(node),
    "remove": lambda client, node: client.node.remove(node),
    "remove-force": lambda client, node: client.node.remove([node], force=True),
}


@pytest.mark.parametrize("mutation", list(_NODE_MUTATIONS))
def test_mutating_call_releases_only_that_nodes_pin(mutation: str) -> None:
    ids = _node_ids(2)
    cli = FakeCli("node", ids)
    with fake_daemon(cli) as client:
        mutated, untouched = client.node.list(hydrate=True)
        _NODE_MUTATIONS[mutation](client, mutated)
        before = len(cli.inspects())
        assert untouched.status is not None
        assert len(cli.inspects()) == before
        assert mutated.status is not None
        assert len(cli.inspects()) == before + 1


@pytest.mark.parametrize("mutation", list(_NODE_MUTATIONS))
def test_failed_mutation_still_releases_the_nodes_pin(mutation: str) -> None:
    ids = _node_ids(1)
    cli = FakeCli("node", ids)
    failing = cli.failing({"update", "promote", "demote", "remove"}, "command")
    with fake_daemon(cli) as client:
        (node,) = client.node.list(hydrate=True)
        with (
            patch("subprocess.run", side_effect=failing),
            pytest.raises(DockerException),
        ):
            _NODE_MUTATIONS[mutation](client, node)
        before = len(cli.inspects())
        assert node.status is not None
    assert len(cli.inspects()) == before + 1


def test_copied_hydrated_node_and_batch_inspected_node_hold_no_pin() -> None:
    ids = _node_ids(1)
    cli = FakeCli("node", ids)
    with fake_daemon(cli) as client:
        (node,) = client.node.list(hydrate=True)
        assert node._pinned is True
        assert copy.copy(node)._pinned is False
        assert pickle.loads(pickle.dumps(node))._pinned is False
        (inspected,) = client.node.inspect(ids)
        assert inspected._pinned is False
