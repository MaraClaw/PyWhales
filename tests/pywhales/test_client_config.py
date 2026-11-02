import json
import sys
import traceback
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from pytest_mock import MockerFixture

from pywhales import Config, DockerClient, Node, Secret, Service, Volume
from pywhales._execution_policy import get_execution_policy
from pywhales.client_config import ClientConfig, ParsingError
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
            resource._last_refreshed_time = datetime.min
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
