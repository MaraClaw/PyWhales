import copy
import inspect
import json
import pickle
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import Config, DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.config.models import ConfigInspectResult
from pywhales.exceptions import (
    DockerException,
    NoSuchConfig,
    NoSuchSecret,
    NoSuchService,
    NotASwarmManager,
)
from pywhales.test_utils import random_name
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


def test_batch_inspect_matches_config_references_and_hydrates_cache() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    alpha = {"ID": "a" * 27, "Spec": {"Name": "alpha", "Data": "YQ=="}}
    bravo = {"ID": "b" * 27, "Spec": {"Name": "bravo", "Data": "Yg=="}}
    references = ["bravo", "a" * 27, "b" * 8, "alpha", "bravo"]
    response = json.dumps([alpha, bravo, alpha, bravo, bravo]).encode()
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 0, response, b"")
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = client.config.inspect(references)
        assert [resource.id for resource in resources] == [
            "b" * 27,
            "a" * 27,
            "b" * 27,
            "a" * 27,
            "b" * 27,
        ]
        assert [
            resource.spec.name for resource in resources if resource.spec is not None
        ] == ["bravo", "alpha", "bravo", "alpha", "bravo"]
        assert all(
            isinstance(resource._get_inspect_result(), ConfigInspectResult)
            for resource in resources
        )
        assert all(
            resource.client_config is client.client_config for resource in resources
        )
        assert resources[0] is not resources[4]
        command.assert_called_once()
        assert command.call_args.args[0] == [
            "docker-test",
            "--host",
            "inventory-host",
            "config",
            "inspect",
            *references,
        ]


def test_batch_inspect_config_scalar_and_lazy_list_contracts() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess(
            [], 0, b'[{"ID":"identity","Spec":{"Name":"named"}}]', b""
        ),
    ) as command:
        resource = client.config.inspect("named")
        assert isinstance(resource, Config)
        assert resource.id == "identity"
        assert isinstance(resource._inspect_result, ConfigInspectResult)
        command.return_value = CompletedProcess([], 0, b"identity", b"")
        listed = client.config.list()
        assert listed[0].id == "identity"
        assert listed[0]._inspect_result is None
        assert command.call_count == 2


def test_batch_inspect_empty_config_avoids_executable_lookup() -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        assert client.config.inspect([]) == []


@pytest.mark.parametrize(
    "payload",
    [
        "invalid json",
        "{}",
        "[]",
        "[null,null]",
        "[{},{}]",
        '[{"ID":"first"}]',
        '[{"ID":"first"},{"ID":"second"},{}]',
        '[{"ID":"wrong"},{"ID":"second"}]',
        '[{"ID":""},{"ID":"second"}]',
        '[{"ID":"first"},{"ID":"second","CreatedAt":"invalid"}]',
        '[{"ID":"one","Spec":{"Name":"first"}},{"ID":"two","Spec":{"Name":"first"}}]',
    ],
)
def test_batch_inspect_config_rejects_invalid_response(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        patch.object(Config, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.config.inspect(["first", "second"])
    hydrate.assert_not_called()


def test_batch_inspect_config_preserves_partial_command_failure() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    output = b'[{"ID":"first"}]'
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 17, output, b"permission denied"),
        ) as command,
        patch.object(Config, "_set_inspect_result") as hydrate,
        pytest.raises(DockerException) as failure,
    ):
        client.config.inspect(["first", "second"])
    assert failure.value.return_code == 17
    assert failure.value.stdout == output.decode()
    assert failure.value.stderr == "permission denied"
    assert failure.value.docker_command == [
        "docker-test",
        "config",
        "inspect",
        "first",
        "second",
    ]
    command.assert_called_once()
    hydrate.assert_not_called()


@pytest.mark.usefixtures("swarm_mode")
def test_list_configs(docker_client: DockerClient) -> None:
    assert docker_client.config.list() == []


@pytest.mark.usefixtures("swarm_mode")
def test_create_delete_config(docker_client: DockerClient, tmp_path: Path) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    my_conf = docker_client.config.create("my_conf", config_file)
    with my_conf:
        assert my_conf.spec is not None
        assert my_conf.spec.name == "my_conf"
        assert docker_client.config.list() == [my_conf]
        assert docker_client.config.inspect("my_conf") == my_conf
        repr(docker_client.config.list())


@pytest.mark.usefixtures("swarm_mode")
def test_labels_config(docker_client: DockerClient, tmp_path: Path) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    my_conf = docker_client.config.create(
        "my_conf", config_file, labels={"dodo": "dada"}
    )
    with my_conf:
        assert my_conf.spec is not None
        assert my_conf.spec.name == "my_conf"
        assert docker_client.config.list(filters={"label": "dodo=dada"}) == [my_conf]
        assert docker_client.config.list(filters={"label": "dodo=dadu"}) == []


@pytest.mark.usefixtures("swarm_mode")
def test_list_configs_with_repeated_name_filter_pairs(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    prefix = random_name()
    with (
        docker_client.config.create(f"{prefix}_a", config_file) as conf_a,
        docker_client.config.create(f"{prefix}_b", config_file) as conf_b,
        docker_client.config.create(f"{prefix}_c", config_file) as conf_c,
    ):
        # Repeated name filters are alternatives, so both selected configs match.
        listed = docker_client.config.list(
            filters=[("name", f"{prefix}_a"), ("name", f"{prefix}_b")]
        )
        assert {conf.id for conf in listed} == {conf_a.id, conf_b.id}
        assert conf_c.id not in {conf.id for conf in listed}
        # A generator is accepted and the Mapping form selects the same config.
        listed = docker_client.config.list(
            filters=(pair for pair in [("name", f"{prefix}_c")])
        )
        assert [conf.id for conf in listed] == [conf_c.id]
        listed = docker_client.config.list(filters={"name": f"{prefix}_c"})
        assert [conf.id for conf in listed] == [conf_c.id]


@pytest.mark.usefixtures("swarm_mode")
def test_list_configs_with_label_pair_filters(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    with docker_client.config.create(
        "my_conf", config_file, labels={"dodo": "dada", "tier": "web"}
    ) as my_conf:
        assert docker_client.config.list(filters=[("label", "dodo=dada")]) == [my_conf]
        assert docker_client.config.list(
            filters=[("label", "dodo=dada"), ("label", "tier=web")]
        ) == [my_conf]
        assert docker_client.config.list(filters=[("label", "dodo=dadu")]) == []


@pytest.mark.usefixtures("swarm_mode")
def test_remove_empty_config_list(docker_client: DockerClient, tmp_path: Path) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    with docker_client.config.create("my_conf", config_file) as my_conf:
        assert docker_client.config.list() == [my_conf]
        docker_client.config.remove([])
        assert docker_client.config.list() == [my_conf]


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
def test_config_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.config.remove(make_empty())


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_config_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.config.remove(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "config", "rm", "alpha"]


NOT_A_MANAGER = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    b"and try again."
)
CONFIG_MISSING = b"Error response from daemon: config resource not found"
CONFIG_JSON = b'[{"ID":"resource","Spec":{"Name":"named"}}]'
# Each failure below is not this config's own not-found error.
OTHER_FAILURES = [
    pytest.param(b"permission denied", DockerException, id="command-failure"),
    pytest.param(NOT_A_MANAGER, NotASwarmManager, id="not-a-swarm-manager"),
    pytest.param(
        b"Error response from daemon: secret resource not found",
        NoSuchSecret,
        id="secret-not-found",
    ),
    pytest.param(
        b"Error response from daemon: service resource not found",
        NoSuchService,
        id="service-not-found",
    ),
]


def _config() -> tuple[DockerClient, Config]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    return client, Config(client.client_config, "resource", is_immutable_id=True)


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_config_cleanup_ignores_only_a_missing_config(missing: bool) -> None:
    _, config = _config()
    response = CompletedProcess(
        [], 1 if missing else 0, b"", CONFIG_MISSING if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, config:
        pass
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "config", "rm", "resource"]


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_config_cleanup_propagates_other_removal_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, config = _config()
    response = CompletedProcess([], 17, b"output", stderr)
    with (
        patch("subprocess.run", return_value=response) as command,
        pytest.raises(DockerException) as raised,
        config,
    ):
        pass
    command.assert_called_once()
    assert type(raised.value) is error_type
    assert raised.value.return_code == 17
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_config_cleanup_failure_joins_the_body_error(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, config = _config()
    body = RuntimeError("body")
    response = CompletedProcess([], 17, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(BaseExceptionGroup) as raised,
        config,
    ):
        raise body
    assert raised.value.message == "Config body and cleanup both failed"
    assert raised.value.exceptions[0] is body
    assert type(raised.value.exceptions[1]) is error_type


def test_config_missing_at_cleanup_leaves_the_body_error_alone() -> None:
    _, config = _config()
    body = RuntimeError("body")
    response = CompletedProcess([], 1, b"", CONFIG_MISSING)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(RuntimeError) as raised,
        config,
    ):
        raise body
    assert raised.value is body


def _exists(client: DockerClient, config: Config, *, as_handle: bool) -> bool:
    return config.exists() if as_handle else client.config.exists("resource")


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_config_exists_is_true_after_inspect(as_handle: bool) -> None:
    client, config = _config()
    response = CompletedProcess([], 0, CONFIG_JSON, b"")
    with patch("subprocess.run", return_value=response) as command:
        found = _exists(client, config, as_handle=as_handle)
    assert found is True
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "config", "inspect", "resource"]


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_config_exists_is_false_for_a_missing_config(as_handle: bool) -> None:
    client, config = _config()
    response = CompletedProcess([], 1, b"", CONFIG_MISSING)
    with patch("subprocess.run", return_value=response):
        found = _exists(client, config, as_handle=as_handle)
    assert found is False


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_config_exists_propagates_other_failures(
    as_handle: bool, stderr: bytes, error_type: type[DockerException]
) -> None:
    client, config = _config()
    response = CompletedProcess([], 1, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(DockerException) as raised,
    ):
        _exists(client, config, as_handle=as_handle)
    assert type(raised.value) is error_type


@pytest.mark.usefixtures("swarm_mode")
def test_config_removed_inside_its_context_exits_cleanly(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    config_file = tmp_path / "config.conf"
    config_file.write_text("hello world")
    with docker_client.config.create("removed_in_context", config_file) as my_conf:
        assert my_conf.exists()
        assert docker_client.config.exists("removed_in_context")
        my_conf.remove()
        assert not my_conf.exists()
    assert not my_conf.exists()
    assert not docker_client.config.exists("removed_in_context")


def _config_ids(count: int) -> list[str]:
    return [identity_of("config", index) for index in range(count)]


def test_hydrated_config_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _config_ids(5)
    cli = FakeCli("config", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        configs = client.config.list(hydrate=True)
        launches = len(cli.launches)
        assert [config.id for config in configs] == ids
        for config in configs:
            assert config._inspect_result is not None
            assert config._inspect_result.id == config.id
        assert [config.spec.name for config in configs if config.spec is not None] == [
            f"con-{index:04d}" for index in range(5)
        ]
    assert launches == 4
    assert cli.lists() == [["docker-test", "config", "list", "--quiet"]]
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]
    assert len(cli.launches) == launches


@pytest.mark.parametrize(
    ("count", "chunks"), [(0, []), (100, [100]), (101, [100, 1])], ids=str
)
def test_hydrated_config_list_inspects_at_most_one_chunk_bound_per_launch(
    count: int, chunks: list[int]
) -> None:
    ids = _config_ids(count)
    cli = FakeCli("config", ids)
    with fake_daemon(cli) as client:
        configs = client.config.list(hydrate=True)
    assert [config.id for config in configs] == ids
    assert [len(batch) for batch in cli.inspects()] == chunks


def test_default_config_list_stays_lazy_and_expiring() -> None:
    ids = _config_ids(3)
    cli = FakeCli("config", ids)
    with fake_daemon(cli) as client:
        configs = client.config.list({"name": "app"})
        assert [config.id for config in configs] == ids
        assert cli.inspects() == []
        assert configs[0].spec is not None
        assert configs[0].spec is not None
        hydrate = inspect.signature(client.config.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.lists() == [
        ["docker-test", "config", "list", "--quiet", "--filter", "name=app"]
    ]
    assert cli.inspects() == [[ids[0]], [ids[0]]]
    assert len(cli.launches) == 3


def test_vanished_configs_reprobe_only_the_affected_chunk() -> None:
    ids = _config_ids(6)
    cli = FakeCli("config", ids)
    cli.vanish_on_list = {ids[4]}
    with fake_daemon(cli, chunk_size=3) as client:
        configs = client.config.list(hydrate=True)
        launches = len(cli.launches)
        assert [config.id for config in configs] == [
            identity for identity in ids if identity != ids[4]
        ]
        assert all(config.spec is not None for config in configs)
    assert len(cli.launches) == launches
    assert cli.inspects() == [
        ids[:3],
        ids[3:],
        ids[3:4],
        ids[4:],
        ids[4:5],
        ids[5:],
    ]


def test_hydrated_config_list_is_empty_when_every_config_vanished() -> None:
    ids = _config_ids(3)
    cli = FakeCli("config", ids)
    cli.vanish_on_list = set(ids)
    with fake_daemon(cli) as client:
        assert client.config.list(hydrate=True) == []
    assert [len(batch) for batch in cli.inspects()] == [3, 1, 2, 1, 1]


@pytest.mark.parametrize("failure", list(INSPECT_FAILURES))
def test_hydrated_config_list_does_not_hide_other_inspect_failures(
    failure: str,
) -> None:
    ids = _config_ids(4)
    cli = FakeCli("config", ids)
    expected = INSPECT_FAILURES[failure]
    with (
        fake_daemon(cli) as client,
        patch("subprocess.run", side_effect=cli.failing({"inspect"}, failure)),
        pytest.raises(expected) as caught,
    ):
        client.config.list(hydrate=True)
    assert type(caught.value) is expected
    assert len(cli.inspects()) == 1


def test_hydrated_config_list_matches_results_by_exact_identity() -> None:
    ids = _config_ids(3)
    cli = FakeCli("config", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for config in client.config.list(hydrate=True):
            assert config._inspect_result is not None
            assert config._inspect_result.id == config.id
        cli.corrupt = {ids[1]: identity_of("config", 99)}
        with pytest.raises(ParsingError):
            client.config.list(hydrate=True)


_CONFIG_MUTATIONS: dict[str, Callable[[DockerClient, Config], object]] = {
    "handle-remove": lambda client, config: config.remove(),
    "cli-remove": lambda client, config: client.config.remove(config),
    "cli-remove-list": lambda client, config: client.config.remove([config]),
}


@pytest.mark.parametrize("mutation", list(_CONFIG_MUTATIONS))
def test_mutating_call_releases_only_that_configs_pin(mutation: str) -> None:
    ids = _config_ids(2)
    cli = FakeCli("config", ids)
    with fake_daemon(cli) as client:
        mutated, untouched = client.config.list(hydrate=True)
        _CONFIG_MUTATIONS[mutation](client, mutated)
        before = len(cli.inspects())
        assert untouched.spec is not None
        assert len(cli.inspects()) == before
        # The fake daemon removed it, so only a refetch can notice that.
        with pytest.raises(NoSuchConfig):
            _ = mutated.spec
        assert len(cli.inspects()) == before + 1


@pytest.mark.parametrize("mutation", list(_CONFIG_MUTATIONS))
def test_failed_mutation_still_releases_the_configs_pin(mutation: str) -> None:
    ids = _config_ids(1)
    cli = FakeCli("config", ids)
    with fake_daemon(cli) as client:
        (config,) = client.config.list(hydrate=True)
        with (
            patch("subprocess.run", side_effect=cli.failing({"rm"}, "command")),
            pytest.raises(DockerException),
        ):
            _CONFIG_MUTATIONS[mutation](client, config)
        before = len(cli.inspects())
        assert config.spec is not None
    assert len(cli.inspects()) == before + 1


def test_copied_hydrated_config_and_batch_inspected_config_hold_no_pin() -> None:
    ids = _config_ids(1)
    cli = FakeCli("config", ids)
    with fake_daemon(cli) as client:
        (config,) = client.config.list(hydrate=True)
        assert config._pinned is True
        assert copy.copy(config)._pinned is False
        assert pickle.loads(pickle.dumps(config))._pinned is False
        (inspected,) = client.config.inspect(ids)
        assert inspected._pinned is False
