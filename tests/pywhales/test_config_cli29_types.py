"""Static config inspect contracts, checked by ty, not pytest."""

from typing import assert_type

from pywhales import Config, DockerClient


def _check_config_inspect(client: DockerClient) -> None:
    assert_type(client.config.inspect("app"), Config)
    assert_type(client.config.inspect(["app"]), list[Config])
    assert_type(client.config.inspect("app", pretty=True), str)
    assert_type(client.config.inspect(["app", "other"], pretty=True), str)
