"""Static contracts for Docker CLI 29 system options; checked by ty, not pytest."""

from typing import assert_type

from pywhales import DockerClient
from pywhales.components.system.cli_wrapper import DiskFreeResult


def _check_disk_free(client: DockerClient, verbose: bool) -> None:
    assert_type(client.system.disk_free(), DiskFreeResult)
    assert_type(client.system.disk_free(verbose=False), DiskFreeResult)
    assert_type(client.system.disk_free(verbose=True), str)
    assert_type(client.system.disk_free(verbose=verbose), DiskFreeResult | str)
