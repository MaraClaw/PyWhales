"""Static secret inspect contracts, checked by ty, not pytest."""

from collections.abc import Iterator
from typing import assert_type

from pywhales import DockerClient, Secret


def _check_secret_inspect(client: DockerClient, references: Iterator[str]) -> None:
    assert_type(client.secret.inspect("app"), Secret)
    assert_type(client.secret.inspect(["app"]), list[Secret])
    assert_type(client.secret.inspect("app", pretty=True), str)
    assert_type(client.secret.inspect(["app", "other"], pretty=True), str)
    assert_type(client.secret.inspect(references, pretty=True), str)
