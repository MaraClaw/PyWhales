import inspect
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Network
from pywhales.client_config import ParsingError
from pywhales.components.container.models import ContainerEndpointIPAMConfig
from pywhales.components.network.cli_wrapper import NetworkInspectResult
from pywhales.exceptions import (
    DockerException,
    NoSuchContainer,
    NoSuchImage,
    NoSuchNetwork,
    NoSuchVolume,
)
from pywhales.test_utils import get_all_jsons, random_name
from tests.pywhales.components.hydration_fakes import (
    FakeCli,
    fake_daemon,
    identity_of,
)


def test_batch_inspect_network_aliases_and_typed_snapshot() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    identity = "a" * 64
    snapshot = {"Id": identity, "Name": "named", "Driver": "bridge"}
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess(
                [], 0, json.dumps([snapshot] * 3).encode(), b""
            ),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        networks = client.network.inspect(["named", identity, identity[:12]])
        assert [network.id for network in networks] == [identity] * 3
        assert all(network.driver == "bridge" for network in networks)
        assert all(
            isinstance(network._inspect_result, NetworkInspectResult)
            for network in networks
        )
        assert command.call_count == 1


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_network_cleanup_preserves_removal(missing: bool) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    network = Network(client.client_config, "resource", is_immutable_id=True)
    response = CompletedProcess(
        [], 1 if missing else 0, b"", b"network resource not found" if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, network:
        pass
    assert command.call_args.args[0] == ["docker-test", "network", "remove", "resource"]
    assert command.call_count == 1
    if missing:
        with (
            patch("subprocess.run", return_value=response),
            pytest.raises(NoSuchNetwork),
        ):
            network.remove()


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        pytest.param(b"permission denied", DockerException, id="command-failure"),
        pytest.param(b"No such container: resource", NoSuchContainer, id="container"),
        pytest.param(b"No such image: resource", NoSuchImage, id="image"),
        pytest.param(b"No such volume: resource", NoSuchVolume, id="volume"),
    ],
)
def test_network_cleanup_propagates_unrelated_cli_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    network = Network(client.client_config, "resource", is_immutable_id=True)
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 17, b"output", stderr)
        ) as command,
        pytest.raises(DockerException) as raised,
        network,
    ):
        pass

    expected = ["docker-test", "network", "remove", "resource"]
    command.assert_called_once()
    assert command.call_args.args[0] == expected
    assert type(raised.value) is error_type
    assert raised.value.docker_command == expected
    assert raised.value.return_code == 17
    assert raised.value.stdout == "output"
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize("json_file", get_all_jsons("networks"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    NetworkInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


def test_network_create_remove(ctr_client: DockerClient) -> None:
    my_name = random_name()
    with ctr_client.network.create(my_name) as my_network:
        assert my_network.name == my_name


def test_network_create_with_labels(ctr_client: DockerClient) -> None:
    my_name = random_name()
    labels = {"hello": "world", "meme": "meme-label"}
    with ctr_client.network.create(my_name, labels=labels) as my_network:
        assert my_network.name == my_name
        assert my_network.labels is not None
        for key, value in labels.items():
            assert my_network.labels[key] == value


def test_context_manager(ctr_client: DockerClient) -> None:
    my_net: Network | None = None
    with (
        pytest.raises(DockerException),
        ctr_client.network.create(random_name()) as created_net,
    ):
        my_net = created_net
        ctr_client.run(
            "busybox",
            ["ping", "idonotexistatall.com"],
            networks=[created_net],
            remove=True,
        )
        # an exception will be raised because the container will fail
        # but the network will be removed anyway.

    assert my_net is not None
    assert my_net not in ctr_client.network.list()


def test_network_connect_disconnect(ctr_client: DockerClient) -> None:
    with (
        ctr_client.network.create(random_name()) as my_net,
        ctr_client.container.run(
            "busybox:1", ["sleep", "infinity"], detach=True
        ) as my_container,
    ):
        ctr_client.network.connect(my_net, my_container)
        ctr_client.network.disconnect(my_net, my_container)


def test_remove_nothing(ctr_client: DockerClient) -> None:
    all_neworks = set(ctr_client.network.list())
    ctr_client.network.remove([])
    assert all_neworks == set(ctr_client.network.list())


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_service_create(docker_client: DockerClient) -> None:
    with (
        docker_client.network.create(
            random_name(), attachable=True, driver="overlay"
        ) as my_net,
        docker_client.service.create(
            "busybox", ["sleep", "infinity"], network=my_net.name
        ),
    ):
        assert my_net.containers is not None
        assert len(my_net.containers) == 2  # 1 container + 1 endpoint


def test_network_exists(ctr_client: DockerClient) -> None:
    network_name = random_name()
    assert not ctr_client.network.exists(network_name)
    with ctr_client.network.create(network_name) as network:
        assert ctr_client.network.exists(network_name)
        assert ctr_client.network.exists(network)
        assert network.exists()

    # Outside with clause, network should be removed and no longer exist
    assert not ctr_client.network.exists(network)
    assert not network.exists()


def test_list_filters(ctr_client: DockerClient) -> None:
    name = random_name()
    with ctr_client.network.create(name) as network:
        networks_listed = ctr_client.network.list(filters=[("name", name)])
    assert networks_listed == [network]


def test_list_filters_client(docker_client: DockerClient) -> None:
    name = random_name()
    with docker_client.network.create(name) as network:
        networks_listed = docker_client.network.list(filters=[("name", name)])
    assert networks_listed == [network]


def test_correct_ipam_config(docker_client: DockerClient) -> None:
    """Verify a fixed container IPv4 address survives network IPAM inspection."""
    with (
        docker_client.network.create(random_name(), subnet="192.0.2.0/24") as net,
        docker_client.container.run(
            "busybox",
            ["sleep", "infinity"],
            detach=True,
            networks=[net],
            ip="192.0.2.5",
        ) as ctr,
    ):
        inspect = docker_client.container.inspect(ctr)
        assert inspect.network_settings is not None
        networks = inspect.network_settings.networks
        assert networks is not None
        assert net.name is not None
        assert networks[net.name].ip_address == "192.0.2.5"
        assert networks[net.name].ipam_config == ContainerEndpointIPAMConfig(
            ipv4_address="192.0.2.5", ipv6_address=None, link_local_ips=None
        )


EMPTY_SELECTIONS = [
    pytest.param(list, id="list"),
    pytest.param(tuple, id="tuple"),
    pytest.param(lambda: (item for item in ()), id="generator"),
]
SINGLE_SELECTIONS = [
    pytest.param(lambda: "alpha", id="str"),
    pytest.param(lambda: ["alpha"], id="list"),
    pytest.param(lambda: (item for item in ["alpha"]), id="generator"),
]


@pytest.mark.parametrize("make_empty", EMPTY_SELECTIONS)
def test_network_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.network.remove(make_empty())


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_network_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.network.remove(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "network", "remove", "alpha"]


def _network_ids(count: int) -> list[str]:
    return [identity_of("network", index) for index in range(count)]


def test_hydrated_network_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _network_ids(5)
    cli = FakeCli("network", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        resources = client.network.list(hydrate=True)
        launches = len(cli.launches)
        assert [str(resource) for resource in resources] == ids
        assert all(resource.driver is not None for resource in resources)
    assert launches == 4
    assert len(cli.launches) == launches
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]


def test_default_network_list_stays_lazy_and_expiring() -> None:
    ids = _network_ids(3)
    cli = FakeCli("network", ids)
    with fake_daemon(cli) as client:
        resources = client.network.list()
        assert cli.inspects() == []
        assert resources[0].driver is not None
        assert resources[0].driver is not None
        hydrate = inspect.signature(client.network.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.inspects() == [[ids[0]], [ids[0]]]


def test_hydrated_network_list_reprobes_only_the_chunk_with_a_vanished_resource() -> (
    None
):
    ids = _network_ids(4)
    cli = FakeCli("network", ids)
    cli.vanish_on_list = {ids[2]}
    with fake_daemon(cli, chunk_size=2) as client:
        resources = client.network.list(hydrate=True)
        launches = len(cli.launches)
        assert [str(resource) for resource in resources] == [ids[0], ids[1], ids[3]]
        assert all(resource.driver is not None for resource in resources)
    assert len(cli.launches) == launches
    assert cli.inspects() == [ids[:2], ids[2:], [ids[2]], [ids[3]]]


def test_hydrated_network_list_matches_results_by_exact_identity() -> None:
    ids = _network_ids(3)
    cli = FakeCli("network", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for resource in client.network.list(hydrate=True):
            assert resource._inspect_result is not None
            assert resource._inspect_result.id == str(resource)
        cli.corrupt = {ids[1]: "other"}
        with pytest.raises(ParsingError):
            client.network.list(hydrate=True)


def test_reload_and_mutating_calls_release_the_pin_of_a_hydrated_network() -> None:
    ids = _network_ids(3)
    cli = FakeCli("network", ids)
    with fake_daemon(cli) as client:
        reloaded, removed, untouched = client.network.list(hydrate=True)
        before = len(cli.launches)
        assert None not in (reloaded.driver, removed.driver, untouched.driver)
        assert len(cli.launches) == before
        reloaded.reload()
        assert reloaded.driver is not None
        assert len(cli.launches) == before + 2
        assert untouched.driver is not None
        assert len(cli.launches) == before + 2
        for mutate in (
            lambda: removed.remove(),
            lambda: client.network.connect(removed, "box"),
            lambda: client.network.disconnect(removed, "box"),
        ):
            mutate()
            used = len(cli.launches)
            assert removed.driver is not None
            assert len(cli.launches) == used + 1
            assert removed.driver is not None
            assert len(cli.launches) == used + 2
