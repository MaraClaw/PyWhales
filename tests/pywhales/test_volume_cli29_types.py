"""Static contracts for Docker CLI 29 volume options; checked by ty, not pytest."""

from collections.abc import Iterable
from typing import assert_type

from pywhales import DockerClient, Volume


def _check_volume_create(client: DockerClient, topologies: Iterable[str]) -> None:
    assert_type(
        client.volume.create(
            "vol",
            "csi",
            {},
            {},
            type="mount",
            scope="multi",
            sharing="onewriter",
            availability="drain",
            group="grp",
            secrets={"key": "secret"},
            limit_bytes=1024,
            required_bytes="2g",
            topology_preferred=topologies,
            topology_required=["zone=a"],
        ),
        Volume,
    )
    assert_type(client.volume.create(type="block", limit_bytes=None), Volume)


def _check_volume_list(client: DockerClient) -> None:
    assert_type(client.volume.list(), list[Volume])
    assert_type(client.volume.list(cluster=True), list[Volume])
    assert_type(
        client.volume.list([("driver", "csi")], hydrate=True, cluster=True),
        list[Volume],
    )


def _check_volume_remove(client: DockerClient, volume: Volume) -> None:
    assert_type(client.volume.remove(volume, force=True), None)
    assert_type(client.volume.remove(["vol"]), None)
    assert_type(volume.remove(force=True), None)
    assert_type(volume.remove(), None)


def _check_volume_update(client: DockerClient, volume: Volume) -> None:
    assert_type(client.volume.update("vol", availability="active"), None)
    assert_type(client.volume.update(volume, availability="pause", timeout=5), None)
    assert_type(volume.update(availability="drain"), None)
    assert_type(volume.update(availability="drain", timeout=None), None)
