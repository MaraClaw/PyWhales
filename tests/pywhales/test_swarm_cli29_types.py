"""Static swarm init and update contracts, checked by ty, not pytest."""

from collections.abc import Iterator
from datetime import timedelta
from typing import assert_type

from pywhales import DockerClient


def _check_swarm_init(client: DockerClient, pools: Iterator[str]) -> None:
    assert_type(client.swarm.init(), None)
    assert_type(
        client.swarm.init("10.0.0.1", True, "drain", "eth0", 4789, "0.0.0.0:2377"),
        None,
    )
    assert_type(
        client.swarm.init(
            default_addr_pools=["10.10.0.0/16"],
            default_addr_pool_mask_length=24,
            force_new_cluster=True,
            cert_expiry=timedelta(hours=1),
            dispatcher_heartbeat=5,
            external_ca=("protocol=cfssl,url=https://ca",),
            max_snapshots=0,
            snapshot_interval=10000,
            task_history_limit=5,
        ),
        None,
    )
    assert_type(
        client.swarm.init(
            default_addr_pools="10.10.0.0/16",
            external_ca="protocol=cfssl,url=https://ca",
        ),
        None,
    )
    assert_type(client.swarm.init(default_addr_pools=pools), None)


def _check_swarm_update(client: DockerClient) -> None:
    assert_type(client.swarm.update(external_ca="protocol=cfssl,url=https://ca"), None)
