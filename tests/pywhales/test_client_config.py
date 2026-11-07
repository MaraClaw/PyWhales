import copy
import json
import pickle
import sys
import traceback
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import override
from unittest.mock import patch

import pytest
from pytest_mock import MockerFixture

from pywhales import Config, DockerClient, Node, Secret, Service, Volume
from pywhales._execution_policy import get_execution_policy
from pywhales.client_config import (
    ClientConfig,
    ParsingError,
    ReloadableObject,
)
from pywhales.test_utils import set_cache_validity_period
from pywhales.utils import PROJECT_ROOT


@pytest.mark.parametrize("timeout", [True, False, 0, -1, float("inf"), float("nan")])
def test_default_timeout_rejects_invalid_values_before_lookup(timeout: float) -> None:
    with patch("shutil.which") as lookup, pytest.raises(ValueError):
        ClientConfig(default_timeout=timeout)
    lookup.assert_not_called()


def test_default_timeout_respects_supplied_config() -> None:
    config = ClientConfig(default_timeout=3)
    client = DockerClient(client_config=config, default_timeout=0)
    assert client.client_config is config
    assert client.client_config.default_timeout == 3


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "8", b"8"])
def test_general_stream_budget_rejects_invalid_config_before_lookup(
    budget: int | None,
) -> None:
    with patch("shutil.which") as lookup, pytest.raises(ValueError):
        ClientConfig(stream_buffer_bytes=budget)

    lookup.assert_not_called()


@pytest.mark.parametrize("budget", [None, 1, 1024])
def test_general_stream_budget_preserves_independent_image_override(
    budget: int | None,
) -> None:
    config = ClientConfig(stream_buffer_bytes=budget)

    assert config.stream_buffer_bytes == budget
    assert config.image_stream_buffer_bytes is None
    assert config.raw_diagnostics is False


@pytest.mark.parametrize("raw", [False, True])
def test_inspect_validation_suppresses_dump_and_chain_unless_raw(
    raw: bool, tmp_path: Path
) -> None:
    secret = "inspect-secret-sentinel"
    payload = {**fake_json_message, "Driver": [secret]}
    client = DockerClient(raw_diagnostics=raw)
    with (
        patch(
            "pywhales.components.volume.cli_wrapper.Volume._fetch_inspect_result_json",
            return_value=payload,
        ),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ParsingError) as raised,
    ):
        client.volume.inspect("volume")

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw
    dumps = list(tmp_path.glob("*.json"))
    assert bool(dumps) is raw
    if raw:
        assert json.loads(dumps[0].read_text()) == payload


@pytest.mark.parametrize("raw", [False, True])
def test_batch_inspect_invalid_json_exposes_validation_chain_only_in_raw_mode(
    raw: bool,
) -> None:
    resource = Volume(ClientConfig(raw_diagnostics=raw), "volume", is_immutable_id=True)
    secret = "batch-inspect-secret-sentinel"
    payload = json.dumps([secret])

    with pytest.raises(ParsingError) as raised:
        resource._parse_inspect_results(payload, ["volume"])

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw


def test_docker_command_policy_does_not_replace_cached_executable_prefix() -> None:
    config = ClientConfig(
        client_call=[sys.executable, "engine"],
        stream_buffer_bytes=17,
        raw_diagnostics=True,
    )
    cached = config.get_client_call_with_path()

    command = config.docker_cmd
    command.append("version")
    config.stream_buffer_bytes = 31

    assert config.get_client_call_with_path() is cached
    assert cached == [Path(sys.executable), "engine"]
    assert isinstance(cached[0], Path)
    assert isinstance(command[0], str)
    assert get_execution_policy(command).stream_buffer_bytes == 31
    assert get_execution_policy(command).raw_diagnostics is True


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "8", b"8"])
def test_image_stream_budget_rejects_invalid_direct_config_before_lookup(
    budget: int | None,
) -> None:
    with patch("shutil.which") as lookup, pytest.raises(ValueError):
        ClientConfig(image_stream_buffer_bytes=budget)

    lookup.assert_not_called()


@pytest.mark.parametrize("budget", [None, 1, 1024])
def test_image_stream_budget_keeps_valid_direct_config_without_lookup(
    budget: int | None,
) -> None:
    with patch("shutil.which") as lookup:
        config = ClientConfig(image_stream_buffer_bytes=budget)

    assert config.image_stream_buffer_bytes == budget
    lookup.assert_not_called()


@pytest.mark.parametrize("component", ["service", "node", "config", "secret"])
@pytest.mark.parametrize("explicit_reload", [False, True], ids=["expired", "explicit"])
def test_swarm_batch_reload_retains_originating_configuration(
    component: str, explicit_reload: bool
) -> None:
    client = DockerClient(context="inventory-context", host="inventory-host", tls=True)
    client.client_config._client_call_with_path = ["docker-test", "--custom-wrapper"]
    api = {
        "service": client.service,
        "node": client.node,
        "config": client.config,
        "secret": client.secret,
    }[component]
    snapshot = {
        "ID": "a" * 27,
        "Spec": {"Name": "named", "Labels": {"version": "before"}},
        "Description": {"Hostname": "named"},
    }
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps([snapshot]).encode(), b""),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resource = api.inspect(["named"])[0]
        assert resource.spec is not None
        assert resource.spec.labels == {"version": "before"}
        initial = resource._get_inspect_result()
        assert resource.client_config is client.client_config
        command.assert_called_once()
        snapshot["Spec"]["Labels"] = {"version": "after"}
        command.return_value = CompletedProcess(
            [], 0, json.dumps([snapshot]).encode(), b""
        )

        if explicit_reload:
            resource.reload()
        else:
            resource._refreshed_at = None
        refreshed = resource._get_inspect_result()

        assert refreshed is not initial
        assert resource.spec is not None
        assert resource.spec.labels == {"version": "after"}
        assert command.call_count == 2
        assert command.call_args.args[0] == [
            "docker-test",
            "--custom-wrapper",
            "--context",
            "inventory-context",
            "--host",
            "inventory-host",
            "--tls",
            component,
            "inspect",
            "a" * 27,
        ]


@pytest.mark.parametrize("component", ["service", "node", "config", "secret"])
@pytest.mark.parametrize(
    ("references", "payload"),
    [
        (["shared", "other"], [{"ID": "shared-one"}, {"ID": "shared-two"}]),
        (
            ["named", "named"],
            [
                {
                    "ID": "one",
                    "Spec": {"Name": "named"},
                    "Description": {"Hostname": "named"},
                },
                {
                    "ID": "two",
                    "Spec": {"Name": "named"},
                    "Description": {"Hostname": "named"},
                },
            ],
        ),
        (["one", "one"], [{"ID": "one"}, {"ID": "two"}]),
        (["one", "two", "two"], [{"ID": "one"}, {"ID": "one"}, {"ID": "two"}]),
        (
            ["one", "one"],
            [
                {"ID": "one", "Spec": {"Name": "before"}},
                {"ID": "one", "Spec": {"Name": "after"}},
            ],
        ),
        (["", "two"], [{"ID": "one"}, {"ID": "two"}]),
    ],
    ids=[
        "ambiguous-prefix",
        "ambiguous-duplicate-name",
        "unrequested-identity",
        "wrong-multiplicity",
        "conflicting-snapshot",
        "empty-reference",
    ],
)
def test_swarm_batch_rejects_ambiguous_or_mismatched_identities_before_hydration(
    component: str,
    references: list[str],
    payload: list[dict[str, str | dict[str, str]]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "service": client.service,
        "node": client.node,
        "config": client.config,
        "secret": client.secret,
    }[component]
    resource_type = {
        "service": Service,
        "node": Node,
        "config": Config,
        "secret": Secret,
    }[component]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps(payload).encode(), b""),
        ) as command,
        patch.object(resource_type, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        api.inspect(references)
    command.assert_called_once()
    hydrate.assert_not_called()


@pytest.mark.parametrize("component", ["service", "node", "config", "secret"])
@pytest.mark.parametrize("batch", [False, True], ids=["scalar", "reordered-batch"])
@pytest.mark.parametrize(
    "collision",
    [
        ("a" * 8, "a" * 8, "other"),
        ("b" * 27, "selected", "b" * 27),
    ],
    ids=["name-over-prefix", "full-id-over-name"],
)
def test_swarm_inspect_uses_docker_identity_precedence(
    component: str, batch: bool, collision: tuple[str, str, str]
) -> None:
    reference, selected_name, other_name = collision
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    api = {
        "service": client.service,
        "node": client.node,
        "config": client.config,
        "secret": client.secret,
    }[component]
    selected = {
        "ID": "b" * 27,
        "Spec": {"Name": selected_name, "Labels": {"match": "selected"}},
        "Description": {"Hostname": selected_name},
    }
    other = {
        "ID": "a" * 27,
        "Spec": {"Name": other_name, "Labels": {"match": "other"}},
        "Description": {"Hostname": other_name},
    }
    references = [reference, "a" * 27, reference] if batch else [reference]
    payload = [other, selected, selected] if batch else [selected]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps(payload).encode(), b""),
        ) as command,
        patch("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600),
    ):
        resources = api.inspect(references) if batch else [api.inspect(reference)]

        assert [resource.id for resource in resources] == (
            ["b" * 27, "a" * 27, "b" * 27] if batch else ["b" * 27]
        )
        assert [
            resource.spec.labels for resource in resources if resource.spec is not None
        ] == (
            [{"match": "selected"}, {"match": "other"}, {"match": "selected"}]
            if batch
            else [{"match": "selected"}]
        )
        assert all(
            resource.client_config is client.client_config for resource in resources
        )
        if batch:
            assert resources[0] is not resources[2]
        command.assert_called_once()
        assert command.call_args.args[0] == [
            "docker-test",
            "--host",
            "inventory-host",
            component,
            "inspect",
            *references,
        ]


fake_json_message = {
    "CreatedAt": "2020-10-08T18:32:55Z",
    "Driver": ["dummy", "fake", ["driver"]],
    "Labels": {"com.docker.stack.namespace": "dodo"},
    "Mountpoint": "/var/lib/docker/volumes/dodo_traefik-data/_data",
    "Name": "dodo_traefik-data",
    "Options": None,
    "Scope": "local",
}


def test_pretty_exception_message_and_report(mocker: MockerFixture) -> None:
    mocker.patch(
        "pywhales.components.volume.cli_wrapper.Volume._fetch_inspect_result_json",
        lambda x, y: fake_json_message,
    )
    with pytest.raises(ParsingError) as err:
        DockerClient(raw_diagnostics=True).volume.inspect("random_volume")
    error_message = str(err.value)

    assert "This is a bug with pywhales itself" in error_message

    # we get the path of the json file with the dump of the response.
    for word in error_message.split():
        if ".json" in word:
            break
    else:
        raise IndexError

    assert Path(word).read_text() == json.dumps(fake_json_message, indent=2)
    Path(word).unlink()


@pytest.mark.parametrize(
    "compose_env_files",
    [
        ["example1.env", "example2.env"],
        ["example1.env"],
        [],
        ["example1.env", "example1.env", "example2.env"],
    ],
    ids=["multiple_files", "single_file", "empty", "repeated"],
)
def test_compose_env_files(compose_env_files: Sequence[str]) -> None:
    """Preserve every configured env-file argument in input order."""
    client_config = ClientConfig(compose_env_files=compose_env_files)
    # Since we are operating of a list of commands, we first find the indices of the `--env-file` argument, then we check that the next argument is the file name.
    indices = [
        index
        for index, item in enumerate(client_config.docker_compose_cmd)
        if item == "--env-file"
    ]
    assert len(indices) == len(compose_env_files)
    for index, env_file in zip(indices, compose_env_files, strict=True):
        assert client_config.docker_compose_cmd[index + 1] == env_file


@contextmanager
def _create_temp_env_files() -> Generator[tuple[Path, Path]]:
    """Write fixed-name env files on context entry and yield them in order.

    Files live in the checkout's component fixture directory, not a private temp
    directory. Existing contents are overwritten; unlinking occurs only when
    execution resumes normally after the yield, not on an exception in the body.

    Yields:
        The file defining A=1 and B=1, followed by the file overriding B=2.
    """
    path = PROJECT_ROOT / "tests/pywhales/components"
    env_file_1 = path / "example1.env"
    with env_file_1.open("w") as f:
        f.write("A=1\n")
        f.write("B=1\n")
    env_file_2 = path / "example2.env"
    with env_file_2.open("w") as f:
        f.write("B=2\n")
    yield env_file_1, env_file_2
    env_file_1.unlink()
    env_file_2.unlink()


def test_run_compose_command_with_env_file() -> None:
    """Verify Compose interpolation defaults and ordered env-file overrides.

    Requires the Docker CLI and Compose plugin to render configuration. Exercise
    a single file and both file orders; later files override earlier B
    values while absent A and B values use the service's zero defaults.
    """
    with _create_temp_env_files() as (env_file_1, env_file_2):
        COMPOSE_FILE = PROJECT_ROOT / "tests/pywhales/components/dummy_compose.yml"
        SERVICE = "service_using_env_variables"

        # Test with no env-file to check the default values, should be `0`, as is default in the service definition.
        client = DockerClient(compose_files=[COMPOSE_FILE])
        config = client.compose.config()
        assert config.services is not None
        environment = config.services[SERVICE].environment
        assert isinstance(environment, dict)
        assert "A" in environment and environment["A"] == "0"
        assert "B" in environment and environment["B"] == "0"

        # Test with single env-file using the new `compose_env_files` argument
        client = DockerClient(
            compose_files=[COMPOSE_FILE],
            compose_env_files=[env_file_1],
        )
        config = client.compose.config()
        assert config.services is not None
        environment = config.services[SERVICE].environment
        assert isinstance(environment, dict)
        assert "A" in environment and environment["A"] == "1"
        assert "B" in environment and environment["B"] == "1"

        # Test with multiple env-files using the new `compose_env_files` argument.
        # Since `env_file2` is loaded after `env_file1`, the value of `B` should be overridden by `env_file_2`.
        client = DockerClient(
            compose_files=[COMPOSE_FILE],
            compose_env_files=[env_file_1, env_file_2],
        )
        config = client.compose.config()
        assert config.services is not None
        environment = config.services[SERVICE].environment
        assert isinstance(environment, dict)
        assert "A" in environment and environment["A"] == "1"
        assert "B" in environment and environment["B"] == "2"

        # Test with multiple env-files using the new `compose_env_files` argument.
        # Since `env_file1` is loaded after `env_file2`, the value of `B` should be overridden by `env_file_1`.
        client = DockerClient(
            compose_files=[COMPOSE_FILE],
            compose_env_files=[env_file_2, env_file_1],
        )
        config = client.compose.config()
        assert config.services is not None
        environment = config.services[SERVICE].environment
        assert isinstance(environment, dict)
        assert "A" in environment and environment["A"] == "1"
        assert "B" in environment and environment["B"] == "1"


def test_strict_env_transport_configuration() -> None:
    assert ClientConfig().strict_env_transport is False
    assert (
        DockerClient(strict_env_transport=True).client_config.strict_env_transport
        is True
    )
    config = ClientConfig(strict_env_transport=True)
    assert DockerClient(client_config=config).client_config is config


def test_strict_workload_policy_is_opt_in_and_snapshotted() -> None:
    assert DockerClient().client_config.strict_workload_errors is False
    client = DockerClient(client_call=[sys.executable], strict_workload_errors=True)
    command = client.client_config.docker_cmd
    snapshot = get_execution_policy(command)
    client.client_config.strict_workload_errors = False
    assert snapshot.strict_workload_errors is True
    assert get_execution_policy(command).strict_workload_errors is False
    assert (
        DockerClient(
            client_config=client.client_config, strict_workload_errors=True
        ).client_config.strict_workload_errors
        is False
    )


@dataclass(frozen=True)
class _Snapshot:
    id: str
    generation: int


class _Handle(ReloadableObject[_Snapshot]):
    """Handle whose inspect is a counter, so launches are the generation."""

    def __init__(self) -> None:
        super().__init__(ClientConfig(), "id", "ref", is_immutable_id=True)
        self.generation = 0

    @override
    def _fetch_and_parse_inspect_result(self, reference: str) -> _Snapshot:
        self.generation += 1
        return _Snapshot(reference, self.generation)


def _published_at(clock: float, *, pinned: bool = False) -> _Handle:
    handle = _Handle()
    with patch("pywhales.client_config.monotonic", return_value=clock):
        if pinned:
            handle._set_pinned_inspect_result(_Snapshot("ref", 0))
        else:
            handle._set_inspect_result(_Snapshot("ref", 0))
    return handle


@pytest.mark.parametrize(
    ("now", "period", "expired"),
    [
        (100.0, 10, False),
        (109.99, 10, False),
        (110.0, 10, True),
        (150.0, 10, True),
        (150.0, 3600, False),
        (99.0, 3600, True),
        (-1e9, 3600, True),
    ],
)
def test_expiry_compares_monotonic_age_with_the_current_validity_period(
    now: float, period: float, expired: bool
) -> None:
    handle = _published_at(100.0)
    with (
        patch("pywhales.client_config.monotonic", return_value=now),
        set_cache_validity_period(period),
    ):
        assert handle._needs_reload() is expired
        handle.generation = 0
        handle._get_inspect_result()
        assert handle.generation == (1 if expired else 0)


def test_unpublished_handle_always_needs_a_reload() -> None:
    assert _Handle()._needs_reload() is True


@pytest.mark.parametrize("wall_clock", [datetime(1970, 1, 1), datetime(2999, 1, 1)])
def test_wall_clock_steps_neither_expire_nor_extend_a_snapshot(
    wall_clock: datetime,
) -> None:
    handle = _published_at(100.0)
    with (
        patch("pywhales.client_config.datetime") as wall,
        patch("pywhales.client_config.monotonic", return_value=100.5),
    ):
        wall.now.return_value = wall_clock
        with set_cache_validity_period(3600):
            assert handle._needs_reload() is False
        with set_cache_validity_period(0.25):
            assert handle._needs_reload() is True


def test_publication_records_wall_clock_for_diagnostics_only() -> None:
    handle = _Handle()
    assert handle._last_refreshed_time == datetime.min
    handle._set_inspect_result(_Snapshot("ref", 0))
    assert handle._last_refreshed_time > datetime.min


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize(
    "transport",
    [lambda handle: pickle.loads(pickle.dumps(handle)), copy.deepcopy, copy.copy],
    ids=["pickle", "deepcopy", "copy"],
)
def test_copied_or_unpickled_handle_resets_freshness_and_pin(
    pinned: bool, transport: Callable[[_Handle], _Handle]
) -> None:
    handle = _published_at(100.0, pinned=pinned)
    clone = transport(handle)
    assert clone._inspect_result == _Snapshot("ref", 0)
    assert clone._last_refreshed_time == datetime.min
    assert clone._refreshed_at is None
    assert clone._pinned is False
    with set_cache_validity_period(3600):
        assert clone._needs_reload() is True
        assert clone._get_inspect_result() == _Snapshot("ref", 1)
        assert clone._needs_reload() is False
    assert handle._pinned is pinned


def test_unpickled_snapshot_expires_even_when_the_new_clock_is_behind() -> None:
    handle = _published_at(1_000_000.0, pinned=True)
    payload = pickle.dumps(handle)
    with (
        patch("pywhales.client_config.monotonic", return_value=5.0),
        set_cache_validity_period(3600),
    ):
        clone = pickle.loads(payload)
        assert clone._needs_reload() is True
        assert clone._get_inspect_result().generation == 1
        assert clone._get_inspect_result().generation == 1


def test_pinned_snapshot_ignores_age_until_reload_or_release() -> None:
    handle = _published_at(100.0, pinned=True)
    with (
        patch("pywhales.client_config.monotonic", return_value=1e9),
        set_cache_validity_period(0),
    ):
        assert handle._get_inspect_result() == _Snapshot("ref", 0)
        assert handle.generation == 0
        handle.reload()
        assert handle._pinned is False
        assert handle.generation == 1
        handle._get_inspect_result()
        assert handle.generation == 2


def test_set_inspect_result_stays_unpinned_and_unpins() -> None:
    handle = _published_at(100.0, pinned=True)
    handle._set_inspect_result(_Snapshot("ref", 7))
    assert handle._pinned is False
    with set_cache_validity_period(0):
        assert handle._get_inspect_result().generation == 1


def test_release_pin_expires_only_a_pinned_snapshot() -> None:
    unpinned = _published_at(100.0)
    unpinned._release_pin()
    assert unpinned._refreshed_at == 100.0
    pinned = _published_at(100.0, pinned=True)
    pinned._release_pin()
    assert pinned._pinned is False
    with (
        patch("pywhales.client_config.monotonic", return_value=100.0),
        set_cache_validity_period(3600),
    ):
        assert pinned._needs_reload() is True
        assert pinned._get_inspect_result().generation == 1


def test_failed_reload_leaves_the_pin_and_snapshot() -> None:
    handle = _published_at(100.0, pinned=True)

    def broken(reference: str) -> _Snapshot:
        raise ParsingError(reference)

    with (
        patch.object(handle, "_fetch_and_parse_inspect_result", broken),
        pytest.raises(ParsingError),
    ):
        handle.reload()
    assert handle._pinned is True
    assert handle._inspect_result == _Snapshot("ref", 0)
