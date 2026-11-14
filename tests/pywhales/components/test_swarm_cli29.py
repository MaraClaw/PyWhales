"""Exact-argv tests for the Docker CLI 29 swarm init and update options.

A recording fake CLI stands in for Docker, so nothing here touches a daemon.
"""

import inspect
from datetime import timedelta
from pathlib import Path

from pywhales.components.swarm.cli_wrapper import SwarmCLI
from tests.pywhales.components.argv_fakes import calls, make_fake_cli

INIT_POSITIONALS = [
    "advertise_address",
    "autolock",
    "availability",
    "data_path_address",
    "data_path_port",
    "listen_address",
]
INIT_KEYWORD_ONLY = [
    "default_addr_pools",
    "default_addr_pool_mask_length",
    "force_new_cluster",
    "cert_expiry",
    "dispatcher_heartbeat",
    "external_ca",
    "max_snapshots",
    "snapshot_interval",
    "task_history_limit",
]


def test_init_keeps_existing_positionals_and_adds_keyword_only_options() -> None:
    parameters = inspect.signature(SwarmCLI.init).parameters
    names = list(parameters)
    assert names[1 : 1 + len(INIT_POSITIONALS)] == INIT_POSITIONALS
    assert all(
        parameters[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name in INIT_POSITIONALS
    )
    assert names[1 + len(INIT_POSITIONALS) :] == INIT_KEYWORD_ONLY
    assert all(
        parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in INIT_KEYWORD_ONLY
    )


def test_init_without_new_options_keeps_its_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.init()
    assert calls(log) == [["swarm", "init", "--availability", "active"]]


def test_init_forwards_every_new_option_after_the_existing_ones(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.init(
        "10.0.0.1",
        True,
        "drain",
        "eth1",
        4789,
        "0.0.0.0:2377",
        default_addr_pools=["10.10.0.0/16", "10.20.0.0/16"],
        default_addr_pool_mask_length=24,
        force_new_cluster=True,
        cert_expiry=timedelta(hours=90),
        dispatcher_heartbeat=7,
        external_ca=[
            "protocol=cfssl,url=https://ca-a",
            "protocol=cfssl,url=https://ca-b",
        ],
        max_snapshots=3,
        snapshot_interval=5000,
        task_history_limit=9,
    )
    assert calls(log) == [
        [
            "swarm",
            "init",
            "--advertise-addr",
            "10.0.0.1",
            "--autolock",
            "--availability",
            "drain",
            "--data-path-addr",
            "eth1",
            "--data-path-port",
            "4789",
            "--listen-addr",
            "0.0.0.0:2377",
            "--cert-expiry",
            "324000.0s",
            "--default-addr-pool",
            "10.10.0.0/16",
            "--default-addr-pool",
            "10.20.0.0/16",
            "--default-addr-pool-mask-length",
            "24",
            "--dispatcher-heartbeat",
            "7s",
            "--external-ca",
            "protocol=cfssl,url=https://ca-a",
            "--external-ca",
            "protocol=cfssl,url=https://ca-b",
            "--force-new-cluster",
            "--max-snapshots",
            "3",
            "--snapshot-interval",
            "5000",
            "--task-history-limit",
            "9",
        ]
    ]


def test_init_accepts_single_values_for_repeated_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.init(
        availability="pause",
        default_addr_pools="10.10.0.0/16",
        external_ca="protocol=cfssl,url=https://ca",
    )
    assert calls(log) == [
        [
            "swarm",
            "init",
            "--availability",
            "pause",
            "--default-addr-pool",
            "10.10.0.0/16",
            "--external-ca",
            "protocol=cfssl,url=https://ca",
        ]
    ]


def test_init_reads_a_generator_of_pools_once(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    pools = (pool for pool in ["10.10.0.0/16", "10.20.0.0/16"])
    client.swarm.init(default_addr_pools=pools)
    assert calls(log) == [
        [
            "swarm",
            "init",
            "--availability",
            "active",
            "--default-addr-pool",
            "10.10.0.0/16",
            "--default-addr-pool",
            "10.20.0.0/16",
        ]
    ]


def test_init_sends_explicit_zero_values(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.init(
        default_addr_pool_mask_length=0,
        cert_expiry=0,
        dispatcher_heartbeat=timedelta(0),
        max_snapshots=0,
        snapshot_interval=0,
        task_history_limit=0,
    )
    assert calls(log) == [
        [
            "swarm",
            "init",
            "--availability",
            "active",
            "--cert-expiry",
            "0s",
            "--default-addr-pool-mask-length",
            "0",
            "--dispatcher-heartbeat",
            "0.0s",
            "--max-snapshots",
            "0",
            "--snapshot-interval",
            "0",
            "--task-history-limit",
            "0",
        ]
    ]


def test_init_leaves_force_new_cluster_out_by_default(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.init(force_new_cluster=False)
    assert calls(log) == [["swarm", "init", "--availability", "active"]]


def test_update_forwards_external_ca_not_the_misspelled_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.update(external_ca="protocol=cfssl,url=https://ca")
    (argv,) = calls(log)
    assert argv == [
        "swarm",
        "update",
        "--external-ca",
        "protocol=cfssl,url=https://ca",
    ]
    assert "--external-ci" not in argv


def test_update_forwards_every_option_with_the_real_flag_names(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.swarm.update(
        autolock=False,
        cert_expiry=timedelta(seconds=30),
        dispatcher_heartbeat=timedelta(seconds=2),
        external_ca="protocol=cfssl,url=https://ca",
        max_snapshots=1,
        snapshot_interval=2,
        task_history_limit=3,
    )
    assert calls(log) == [
        [
            "swarm",
            "update",
            "--autolock=false",
            "--cert-expiry",
            "30.0s",
            "--dispatcher-heartbeat",
            "2.0s",
            "--external-ca",
            "protocol=cfssl,url=https://ca",
            "--max-snapshots",
            "1",
            "--snapshot-interval",
            "2",
            "--task-history-limit",
            "3",
        ]
    ]
