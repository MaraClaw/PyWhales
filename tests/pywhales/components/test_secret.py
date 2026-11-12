import copy
import datetime as dt
import inspect
import json
import pickle
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Secret
from pywhales.client_config import ParsingError
from pywhales.components.secret.models import SecretInspectResult
from pywhales.exceptions import (
    DockerException,
    NoSuchConfig,
    NoSuchService,
    NotASwarmManager,
)
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


def test_batch_inspect_matches_secret_single_pass_references_and_hydrates_cache() -> (
    None
):
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    alpha = {"ID": "a" * 27, "Spec": {"Name": "alpha", "Labels": {"role": "alpha"}}}
    bravo = {"ID": "b" * 27, "Spec": {"Name": "bravo", "Labels": {"role": "bravo"}}}
    references = ["bravo", "a" * 27, "b" * 8, "alpha", "bravo"]
    response = json.dumps([alpha, bravo, alpha, bravo, bravo]).encode()
    with (
        patch(
            "subprocess.run", return_value=CompletedProcess([], 0, response, b"")
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = client.secret.inspect(iter(references))
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
            isinstance(resource._get_inspect_result(), SecretInspectResult)
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
            "secret",
            "inspect",
            *references,
        ]


def test_batch_inspect_secret_scalar_and_lazy_list_contracts() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess(
            [], 0, b'[{"ID":"identity","Spec":{"Name":"named"}}]', b""
        ),
    ) as command:
        resource = client.secret.inspect("named")
        assert isinstance(resource, Secret)
        assert resource.id == "identity"
        assert isinstance(resource._inspect_result, SecretInspectResult)
        command.return_value = CompletedProcess([], 0, b"identity", b"")
        listed = client.secret.list()
        assert listed[0].id == "identity"
        assert listed[0]._inspect_result is None
        assert command.call_count == 2


def test_batch_inspect_empty_secret_avoids_executable_lookup() -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        assert client.secret.inspect([]) == []
        assert client.secret.inspect(iter(())) == []


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
def test_batch_inspect_secret_rejects_invalid_response(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        patch.object(Secret, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.secret.inspect(["first", "second"])
    hydrate.assert_not_called()


def test_batch_inspect_secret_preserves_partial_command_failure() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    output = b'[{"ID":"first"}]'
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 17, output, b"permission denied"),
        ) as command,
        patch.object(Secret, "_set_inspect_result") as hydrate,
        pytest.raises(DockerException) as failure,
    ):
        client.secret.inspect(["first", "second"])
    assert failure.value.return_code == 17
    assert failure.value.stdout == output.decode()
    assert failure.value.stderr == "permission denied"
    assert failure.value.docker_command == [
        "docker-test",
        "secret",
        "inspect",
        "first",
        "second",
    ]
    command.assert_called_once()
    hydrate.assert_not_called()


@pytest.mark.usefixtures("swarm_mode")
def test_swarm_update_auto_lock_managers(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("hello")
    my_secret = docker_client.secret.create("my_secret", secret_file)
    date_after_creation = dt.datetime.now(tz=dt.UTC)
    print("my_secret: ", my_secret.id)
    my_secret2 = docker_client.secret.inspect(my_secret.id)

    assert my_secret == my_secret2
    assert my_secret.spec is not None
    assert my_secret.spec.name == "my_secret"
    assert my_secret.spec.labels == {}
    assert my_secret.created_at is not None
    assert my_secret.created_at <= date_after_creation

    assert my_secret2.spec is not None
    assert my_secret2.spec.name == "my_secret"
    assert my_secret2.spec.labels == {}
    assert my_secret2.created_at is not None
    assert my_secret2.created_at <= date_after_creation

    my_secret3 = docker_client.secret.inspect("my_secret")

    assert my_secret == my_secret3
    assert my_secret3.spec is not None
    assert my_secret3.spec.name == "my_secret"
    assert my_secret3.spec.labels == {}
    assert my_secret3.created_at is not None
    assert my_secret3.created_at <= date_after_creation

    docker_client.secret.remove(my_secret.id)


NOT_A_MANAGER = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    b"and try again."
)
SECRET_MISSING = b"Error response from daemon: secret resource not found"
SECRET_JSON = b'[{"ID":"resource","Spec":{"Name":"named"}}]'
# Each failure below is not this secret's own not-found error.
OTHER_FAILURES = [
    pytest.param(b"permission denied", DockerException, id="command-failure"),
    pytest.param(NOT_A_MANAGER, NotASwarmManager, id="not-a-swarm-manager"),
    pytest.param(
        b"Error response from daemon: config resource not found",
        NoSuchConfig,
        id="config-not-found",
    ),
    pytest.param(
        b"Error response from daemon: service resource not found",
        NoSuchService,
        id="service-not-found",
    ),
]


def _secret() -> tuple[DockerClient, Secret]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    return client, Secret(client.client_config, "resource", is_immutable_id=True)


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_secret_cleanup_ignores_only_a_missing_secret(missing: bool) -> None:
    _, secret = _secret()
    response = CompletedProcess(
        [], 1 if missing else 0, b"", SECRET_MISSING if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, secret:
        pass
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "secret", "remove", "resource"]


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_secret_cleanup_propagates_other_removal_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, secret = _secret()
    response = CompletedProcess([], 17, b"output", stderr)
    with (
        patch("subprocess.run", return_value=response) as command,
        pytest.raises(DockerException) as raised,
        secret,
    ):
        pass
    command.assert_called_once()
    assert type(raised.value) is error_type
    assert raised.value.return_code == 17
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_secret_cleanup_failure_joins_the_body_error(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, secret = _secret()
    body = RuntimeError("body")
    response = CompletedProcess([], 17, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(BaseExceptionGroup) as raised,
        secret,
    ):
        raise body
    assert raised.value.message == "Secret body and cleanup both failed"
    assert raised.value.exceptions[0] is body
    assert type(raised.value.exceptions[1]) is error_type


def test_secret_missing_at_cleanup_leaves_the_body_error_alone() -> None:
    _, secret = _secret()
    body = RuntimeError("body")
    response = CompletedProcess([], 1, b"", SECRET_MISSING)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(RuntimeError) as raised,
        secret,
    ):
        raise body
    assert raised.value is body


def _exists(client: DockerClient, secret: Secret, *, as_handle: bool) -> bool:
    return secret.exists() if as_handle else client.secret.exists("resource")


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_secret_exists_is_true_after_inspect(as_handle: bool) -> None:
    client, secret = _secret()
    response = CompletedProcess([], 0, SECRET_JSON, b"")
    with patch("subprocess.run", return_value=response) as command:
        found = _exists(client, secret, as_handle=as_handle)
    assert found is True
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "secret", "inspect", "resource"]


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_secret_exists_is_false_for_a_missing_secret(as_handle: bool) -> None:
    client, secret = _secret()
    response = CompletedProcess([], 1, b"", SECRET_MISSING)
    with patch("subprocess.run", return_value=response):
        found = _exists(client, secret, as_handle=as_handle)
    assert found is False


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_secret_exists_propagates_other_failures(
    as_handle: bool, stderr: bytes, error_type: type[DockerException]
) -> None:
    client, secret = _secret()
    response = CompletedProcess([], 1, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(DockerException) as raised,
    ):
        _exists(client, secret, as_handle=as_handle)
    assert type(raised.value) is error_type


@pytest.mark.usefixtures("swarm_mode")
def test_secret_removed_inside_its_context_exits_cleanly(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("hello")
    with docker_client.secret.create("removed_in_context", secret_file) as my_secret:
        assert my_secret.exists()
        assert docker_client.secret.exists("removed_in_context")
        my_secret.remove()
        assert not my_secret.exists()
    assert not my_secret.exists()
    assert not docker_client.secret.exists("removed_in_context")


def _secret_ids(count: int) -> list[str]:
    return [identity_of("secret", index) for index in range(count)]


def test_hydrated_secret_list_costs_one_listing_plus_one_launch_per_chunk() -> None:
    ids = _secret_ids(5)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli, chunk_size=2) as client:
        secrets = client.secret.list(hydrate=True)
        launches = len(cli.launches)
        assert [secret.id for secret in secrets] == ids
        for secret in secrets:
            assert secret._inspect_result is not None
            assert secret._inspect_result.id == secret.id
        assert [secret.spec.name for secret in secrets if secret.spec is not None] == [
            f"sec-{index:04d}" for index in range(5)
        ]
    assert launches == 4
    assert cli.lists() == [["docker-test", "secret", "list", "--quiet"]]
    assert cli.inspects() == [ids[:2], ids[2:4], ids[4:]]
    assert len(cli.launches) == launches


@pytest.mark.parametrize(
    ("count", "chunks"), [(0, []), (100, [100]), (101, [100, 1])], ids=str
)
def test_hydrated_secret_list_inspects_at_most_one_chunk_bound_per_launch(
    count: int, chunks: list[int]
) -> None:
    ids = _secret_ids(count)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli) as client:
        secrets = client.secret.list(hydrate=True)
    assert [secret.id for secret in secrets] == ids
    assert [len(batch) for batch in cli.inspects()] == chunks


def test_default_secret_list_stays_lazy_and_expiring() -> None:
    ids = _secret_ids(3)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli) as client:
        secrets = client.secret.list([("name", "token")])
        assert [secret.id for secret in secrets] == ids
        assert cli.inspects() == []
        assert secrets[0].spec is not None
        assert secrets[0].spec is not None
        hydrate = inspect.signature(client.secret.list).parameters["hydrate"]
        assert hydrate.kind is inspect.Parameter.KEYWORD_ONLY
        assert hydrate.default is False
    assert cli.lists() == [
        ["docker-test", "secret", "list", "--quiet", "--filter", "name=token"]
    ]
    assert cli.inspects() == [[ids[0]], [ids[0]]]
    assert len(cli.launches) == 3


def test_vanished_secrets_reprobe_only_the_affected_chunk() -> None:
    ids = _secret_ids(6)
    cli = FakeCli("secret", ids)
    cli.vanish_on_list = {ids[4]}
    with fake_daemon(cli, chunk_size=3) as client:
        secrets = client.secret.list(hydrate=True)
        launches = len(cli.launches)
        assert [secret.id for secret in secrets] == [
            identity for identity in ids if identity != ids[4]
        ]
        assert all(secret.spec is not None for secret in secrets)
    assert len(cli.launches) == launches
    assert cli.inspects() == [
        ids[:3],
        ids[3:],
        ids[3:4],
        ids[4:],
        ids[4:5],
        ids[5:],
    ]


def test_hydrated_secret_list_is_empty_when_every_secret_vanished() -> None:
    ids = _secret_ids(3)
    cli = FakeCli("secret", ids)
    cli.vanish_on_list = set(ids)
    with fake_daemon(cli) as client:
        assert client.secret.list(hydrate=True) == []
    assert [len(batch) for batch in cli.inspects()] == [3, 1, 2, 1, 1]


@pytest.mark.parametrize("failure", list(INSPECT_FAILURES))
def test_hydrated_secret_list_does_not_hide_other_inspect_failures(
    failure: str,
) -> None:
    ids = _secret_ids(4)
    cli = FakeCli("secret", ids)
    expected = INSPECT_FAILURES[failure]
    with (
        fake_daemon(cli) as client,
        patch("subprocess.run", side_effect=cli.failing({"inspect"}, failure)),
        pytest.raises(expected) as caught,
    ):
        client.secret.list(hydrate=True)
    assert type(caught.value) is expected
    assert len(cli.inspects()) == 1


def test_hydrated_secret_list_matches_results_by_exact_identity() -> None:
    ids = _secret_ids(3)
    cli = FakeCli("secret", ids)
    cli.reorder = True
    with fake_daemon(cli) as client:
        for secret in client.secret.list(hydrate=True):
            assert secret._inspect_result is not None
            assert secret._inspect_result.id == secret.id
        cli.corrupt = {ids[1]: identity_of("secret", 99)}
        with pytest.raises(ParsingError):
            client.secret.list(hydrate=True)


_SECRET_MUTATIONS: dict[str, Callable[[DockerClient, Secret], object]] = {
    "handle-remove": lambda client, secret: secret.remove(),
    "cli-remove": lambda client, secret: client.secret.remove(secret),
    "cli-remove-list": lambda client, secret: client.secret.remove([secret]),
}


@pytest.mark.parametrize("mutation", list(_SECRET_MUTATIONS))
def test_mutating_call_releases_only_that_secrets_pin(mutation: str) -> None:
    ids = _secret_ids(2)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli) as client:
        mutated, untouched = client.secret.list(hydrate=True)
        _SECRET_MUTATIONS[mutation](client, mutated)
        before = len(cli.inspects())
        assert untouched.spec is not None
        assert len(cli.inspects()) == before
        assert mutated.spec is not None
        assert len(cli.inspects()) == before + 1


@pytest.mark.parametrize("mutation", list(_SECRET_MUTATIONS))
def test_failed_mutation_still_releases_the_secrets_pin(mutation: str) -> None:
    ids = _secret_ids(1)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli) as client:
        (secret,) = client.secret.list(hydrate=True)
        with (
            patch("subprocess.run", side_effect=cli.failing({"remove"}, "command")),
            pytest.raises(DockerException),
        ):
            _SECRET_MUTATIONS[mutation](client, secret)
        before = len(cli.inspects())
        assert secret.spec is not None
    assert len(cli.inspects()) == before + 1


def test_copied_hydrated_secret_and_batch_inspected_secret_hold_no_pin() -> None:
    ids = _secret_ids(1)
    cli = FakeCli("secret", ids)
    with fake_daemon(cli) as client:
        (secret,) = client.secret.list(hydrate=True)
        assert secret._pinned is True
        assert copy.copy(secret)._pinned is False
        assert pickle.loads(pickle.dumps(secret))._pinned is False
        (inspected,) = client.secret.inspect(ids)
        assert inspected._pinned is False
