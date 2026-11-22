"""Static node inspect, list and ps contracts, checked by ty, not pytest."""

from collections.abc import Iterator
from typing import assert_type

from pywhales import DockerClient, Node, Task, enums
from pywhales.components.node.cli_wrapper import NodeListFilter, NodePsFilter


def _check_node_inspect(client: DockerClient) -> None:
    assert_type(client.node.inspect("self"), Node)
    assert_type(client.node.inspect(["self"]), list[Node])
    assert_type(client.node.inspect("self", pretty=True), str)
    assert_type(client.node.inspect(["self", "worker"], pretty=True), str)


def _check_node_list(
    client: DockerClient, pairs: Iterator[NodeListFilter], mapping: dict[str, str]
) -> None:
    assert_type(client.node.list(), list[Node])
    assert_type(client.node.list(hydrate=True), list[Node])
    assert_type(
        client.node.list([("role", "manager"), ("node.label", "r=1")]), list[Node]
    )
    assert_type(client.node.list(filters=mapping), list[Node])
    assert_type(client.node.list(pairs, hydrate=True), list[Node])


def _check_node_ps(
    client: DockerClient, node: Node, pairs: Iterator[NodePsFilter]
) -> None:
    assert_type(client.node.ps(), list[Task])
    assert_type(
        client.node.ps("self", filters=[("desired-state", "running")]), list[Task]
    )
    assert_type(client.node.ps([node], filters={"name": "web"}), list[Task])
    assert_type(client.node.ps(filters=pairs), list[Task])
    assert_type(node.ps(), list[Task])
    assert_type(node.ps(filters=[("label", "usage")]), list[Task])


def _check_node_update(client: DockerClient, node: Node) -> None:
    """Check updates accept literal and enum options or explicit omission."""
    assert_type(client.node.update("self", "pause", {}, (), "manager"), None)
    assert_type(
        client.node.update(
            node, availability=enums.NodeAvailability.DRAIN, role=enums.NodeRole.WORKER
        ),
        None,
    )
    assert_type(client.node.update("self", availability=None, role=None), None)
    assert_type(node.update("active", {}, (), "worker"), None)
    assert_type(
        node.update(
            availability=enums.NodeAvailability.PAUSE, role=enums.NodeRole.MANAGER
        ),
        None,
    )
    assert_type(node.update(availability=None, role=None), None)
