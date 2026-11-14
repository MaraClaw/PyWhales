import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pywhales import DockerClient
from pywhales.components.system.models import SwarmSpec
from pywhales.exceptions import NotASwarmManager


@pytest.fixture
def swarm_transport() -> Iterator[tuple[DockerClient, MagicMock]]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["fake-docker", "prefix"]
    with patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, b"token\n", b""),
    ) as transport:
        assert isinstance(transport, MagicMock)
        yield client, transport


@pytest.mark.parametrize("detach", [False, True])
def test_ca_reads_certificate_only_when_attached(
    swarm_transport: tuple[DockerClient, MagicMock], detach: bool
) -> None:
    client, transport = swarm_transport
    transport.side_effect = [
        subprocess.CompletedProcess([], 0, b"rotation progress\n", b""),
        subprocess.CompletedProcess([], 0, b"certificate\n", b""),
    ]

    result = client.swarm.ca(
        ca_certificate=Path("cert.pem"),
        ca_key=Path("key.pem"),
        certificate_expiry=timedelta(seconds=1.5),
        detach=detach,
        external_ca="protocol=cfssl",
        rotate=True,
    )

    assert result == (None if detach else "certificate")
    calls = transport.call_args_list
    assert len(calls) == (1 if detach else 2)
    assert calls[0].args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "ca",
        "--ca-cert",
        "cert.pem",
        "--ca-key",
        "key.pem",
        "--cert-expiry",
        "1.5s",
        *(["--detach"] if detach else []),
        "--external-ca",
        "protocol=cfssl",
        "--rotate",
    ]
    if not detach:
        assert calls[1].args[0] == ["fake-docker", "prefix", "swarm", "ca"]


@pytest.mark.parametrize("expiry,expected", [(None, []), (0, ["--cert-expiry", "0s"])])
def test_ca_duration_omission_and_zero(
    swarm_transport: tuple[DockerClient, MagicMock],
    expiry: int | None,
    expected: list[str],
) -> None:
    client, transport = swarm_transport
    client.swarm.ca(certificate_expiry=expiry, detach=True)
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "ca",
        *expected,
        "--detach",
    ]


def test_init_serializes_options(
    swarm_transport: tuple[DockerClient, MagicMock],
) -> None:
    client, transport = swarm_transport
    client.swarm.init(
        advertise_address="10.0.0.1",
        autolock=True,
        availability="drain",
        data_path_address="eth0",
        data_path_port=0,
        listen_address="0.0.0.0:2377",
    )
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "init",
        "--advertise-addr",
        "10.0.0.1",
        "--autolock",
        "--availability",
        "drain",
        "--data-path-addr",
        "eth0",
        "--data-path-port",
        "0",
        "--listen-addr",
        "0.0.0.0:2377",
    ]


def test_join_serializes_options(
    swarm_transport: tuple[DockerClient, MagicMock],
) -> None:
    client, transport = swarm_transport
    client.swarm.join(
        "manager:2377",
        advertise_address="eth0",
        availability="pause",
        data_path_address="eth1",
        listen_address="0.0.0.0:2377",
        token="secret",
    )
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "join",
        "--advertise-addr",
        "eth0",
        "--availability",
        "pause",
        "--data-path-addr",
        "eth1",
        "--listen-addr",
        "0.0.0.0:2377",
        "--token",
        "secret",
        "manager:2377",
    ]


@pytest.mark.parametrize("rotate", [False, True])
def test_join_token_flags(
    swarm_transport: tuple[DockerClient, MagicMock], rotate: bool
) -> None:
    client, transport = swarm_transport
    assert client.swarm.join_token("worker", rotate=rotate) == "token"
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "join-token",
        "--quiet",
        *(["--rotate"] if rotate else []),
        "worker",
    ]


@pytest.mark.parametrize("rotate", [False, True])
def test_unlock_key_flags(
    swarm_transport: tuple[DockerClient, MagicMock], rotate: bool
) -> None:
    client, transport = swarm_transport
    assert client.swarm.unlock_key(rotate=rotate) == "token"
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "unlock-key",
        "--quiet",
        *(["--rotate"] if rotate else []),
    ]


@pytest.mark.parametrize("force", [False, True])
def test_leave_force_serialization(
    swarm_transport: tuple[DockerClient, MagicMock], force: bool
) -> None:
    client, transport = swarm_transport
    client.swarm.leave(force=force)
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "leave",
        *(["--force"] if force else []),
    ]


def test_unlock_transmits_utf8_to_child(tmp_path: Path) -> None:
    received = tmp_path / "stdin"
    client = DockerClient()
    client.client_config._client_call_with_path = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
        str(received),
    ]
    client.swarm.unlock("key-\u00e9")
    assert received.read_bytes() == b"key-\xc3\xa9"


@pytest.mark.parametrize(
    "autolock,expected",
    [(None, []), (False, ["--autolock=false"]), (True, ["--autolock=true"])],
)
def test_update_serializes_optional_values(
    swarm_transport: tuple[DockerClient, MagicMock],
    autolock: bool | None,
    expected: list[str],
) -> None:
    client, transport = swarm_transport
    client.swarm.update(
        autolock=autolock,
        cert_expiry=timedelta(0),
        dispatcher_heartbeat=timedelta(seconds=2.5),
        external_ca="endpoint",
        max_snapshots=0,
        snapshot_interval=0,
        task_history_limit=0,
    )
    assert transport.call_args.args[0] == [
        "fake-docker",
        "prefix",
        "swarm",
        "update",
        *expected,
        "--cert-expiry",
        "0.0s",
        "--dispatcher-heartbeat",
        "2.5s",
        "--external-ca",
        "endpoint",
        "--max-snapshots",
        "0",
        "--snapshot-interval",
        "0",
        "--task-history-limit",
        "0",
    ]


def test_update_omits_unspecified_values(
    swarm_transport: tuple[DockerClient, MagicMock],
) -> None:
    client, transport = swarm_transport
    client.swarm.update()
    assert transport.call_args.args[0] == ["fake-docker", "prefix", "swarm", "update"]


def get_cluster_spec(docker_client: DockerClient) -> SwarmSpec:
    swarm = docker_client.system.info().swarm
    assert swarm is not None
    assert swarm.cluster is not None
    assert swarm.cluster.spec is not None
    return swarm.cluster.spec


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_update_retention_limit(docker_client: DockerClient) -> None:
    docker_client.swarm.update(task_history_limit=4)
    orchestration = get_cluster_spec(docker_client).orchestration
    assert orchestration is not None
    assert orchestration.task_history_retention_limit == 4


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_change_certificate_expiry(docker_client: DockerClient) -> None:
    ca = docker_client.swarm.ca(certificate_expiry=timedelta(days=1), rotate=True)
    ca_config = get_cluster_spec(docker_client).ca_config
    assert ca_config is not None
    assert ca_config.node_cert_expiry is not None
    node_cert_expiry = timedelta(microseconds=ca_config.node_cert_expiry / 1000)
    assert node_cert_expiry == timedelta(days=1)
    assert ca is not None
    assert "BEGIN CERTIFICATE" in ca


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_update_auto_lock_managers(docker_client: DockerClient) -> None:
    encryption_config = get_cluster_spec(docker_client).encryption_config
    assert encryption_config is not None
    assert encryption_config.auto_lock_managers is not True
    docker_client.swarm.update(autolock=True)
    encryption_config = get_cluster_spec(docker_client).encryption_config
    assert encryption_config is not None
    assert encryption_config.auto_lock_managers is True


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_unlock_key(docker_client: DockerClient) -> None:
    docker_client.swarm.update(autolock=True)
    first_key = docker_client.swarm.unlock_key()
    # make sure is doesn't change
    assert first_key == docker_client.swarm.unlock_key()

    # make sure it changes:
    assert first_key != docker_client.swarm.unlock_key(rotate=True)


def test_swarm_join_token_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.swarm.join_token("manager")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_update_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.swarm.update(autolock=True)

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_unlock_key_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.swarm.unlock_key()

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()
