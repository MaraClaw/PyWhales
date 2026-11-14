"""Static contracts for Docker CLI 29 manifest options; checked by ty, not pytest."""

from typing import assert_type

from pywhales import DockerClient
from pywhales.components.manifest.cli_wrapper import ManifestList


def _check_manifest_inspect(client: DockerClient, verbose: bool) -> None:
    assert_type(client.manifest.inspect("alpine"), ManifestList)
    assert_type(client.manifest.inspect("alpine", True), ManifestList)
    assert_type(client.manifest.inspect("alpine", verbose=False), ManifestList)
    assert_type(client.manifest.inspect("alpine", verbose=True), str)
    assert_type(client.manifest.inspect("alpine", True, verbose=True), str)
    assert_type(client.manifest.inspect("alpine", verbose=verbose), ManifestList | str)


def _check_manifest_push(client: DockerClient) -> None:
    assert_type(client.manifest.push("list"), None)
    assert_type(client.manifest.push("list", insecure=True), None)
    assert_type(
        client.manifest.push("list", True, True, timeout=5, insecure=True), None
    )
