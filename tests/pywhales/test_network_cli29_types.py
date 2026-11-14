"""Static contracts for Docker CLI 29 network options; checked by ty, not pytest."""

from collections.abc import Iterable
from typing import assert_type

from pywhales import DockerClient, Network


def _check_network_connect(client: DockerClient, ips: Iterable[str]) -> None:
    assert_type(
        client.network.connect("net", "ctr", gw_priority=10, link_local_ips=ips), None
    )
    assert_type(client.network.connect("net", "ctr", gw_priority=None), None)


def _check_network_create(client: DockerClient) -> None:
    assert_type(
        client.network.create(
            "net",
            config_from=None,
            config_only=True,
            ingress=False,
            ipv4=None,
            scope="swarm",
        ),
        Network,
    )
    assert_type(
        client.network.create("net", config_from="template", ipv4=False, scope="local"),
        Network,
    )


def _check_network_inspect(client: DockerClient, references: list[str]) -> None:
    assert_type(client.network.inspect("net"), Network)
    assert_type(client.network.inspect("net", verbose=False), Network)
    assert_type(client.network.inspect(references), list[Network])
    assert_type(client.network.inspect(references, verbose=False), list[Network])
    assert_type(client.network.inspect("net", verbose=True), str)
    assert_type(client.network.inspect(references, verbose=True), str)
    assert_type(client.network.inspect(iter(references), verbose=True), str)


def _check_network_remove(client: DockerClient, network: Network) -> None:
    assert_type(client.network.remove(network, force=True), None)
    assert_type(client.network.remove(["net"]), None)
    assert_type(network.remove(force=True), None)
    assert_type(network.remove(), None)
