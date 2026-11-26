import json
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components.plugin.cli_wrapper import Plugin
from pywhales.components.plugin.models import PluginInspectResult
from pywhales.exceptions import DockerException, NoSuchContainer, NotASwarmManager
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.repr_fakes import assert_handle_reprs


def test_plugin_repr_uses_only_held_snapshot() -> None:
    identity = "0123456789abcdef" * 4
    config = ClientConfig(_client_call_with_path=["docker-test"])
    handle = Plugin(config, identity, is_immutable_id=True)
    snapshot = PluginInspectResult(id=identity, name="repo/plugin:latest")
    assert_handle_reprs(
        handle,
        snapshot,
        lazy="pywhales.Plugin(id='0123456789ab', name='<not loaded>')",
        loaded="pywhales.Plugin(id='0123456789ab', name='repo/plugin:latest')",
        partial="pywhales.Plugin(id='None', name='<not loaded>')",
    )


test_plugin_name = "vieux/sshfs:latest"


def get_all_plugins_jsons() -> list[Path]:
    jsons_directory = Path(__file__).parent / "plugins"
    return sorted(jsons_directory.iterdir())


@pytest.mark.parametrize("json_file", get_all_jsons("plugins"))
def test_load_json(json_file: Path) -> None:
    json_as_txt = json_file.read_text()
    PluginInspectResult(**json.loads(json_as_txt))
    # we could do more checks here if needed


def test_install_plugin_disable_enable(docker_client: DockerClient) -> None:
    with docker_client.plugin.install(test_plugin_name) as my_plugin:
        my_plugin.disable()
        my_plugin.enable()
        assert my_plugin in docker_client.plugin.list()
        assert "name='vieux/sshfs:latest'" in repr(my_plugin)


def test_plugin_upgrade(docker_client: DockerClient) -> None:
    with docker_client.plugin.install(test_plugin_name) as my_plugin:
        my_plugin.disable()
        my_plugin.upgrade()


def test_remove_empty_plugin_list(docker_client: DockerClient) -> None:
    with docker_client.plugin.install(test_plugin_name) as my_plugin:
        plugins_set = set(docker_client.plugin.list())
        assert my_plugin in plugins_set
        docker_client.plugin.remove([])
        assert set(docker_client.plugin.list()) == plugins_set


def test_batch_inspect_preserves_plugin_cli_aliases_positionally() -> None:
    client = DockerClient(host="inventory-host")
    client.client_config._client_call_with_path = ["docker-test"]
    references = [
        "registry.example/team/plugin",
        "local-alias",
        "registry.example/team/plugin",
    ]
    payload = [
        {"Id": "a" * 64, "Name": "different:latest"},
        {"Id": "b" * 64, "Name": "different:custom"},
        {"Id": "a" * 64, "Name": "different:latest"},
    ]
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, json.dumps(payload).encode(), b""),
    ) as command:
        resources = client.plugin.inspect(references)
    assert [resource.id for resource in resources] == ["a" * 64, "b" * 64, "a" * 64]
    assert resources[0] is not resources[2]
    assert all(resource._inspect_result is not None for resource in resources)
    assert all(resource.client_config is client.client_config for resource in resources)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "--host",
        "inventory-host",
        "plugin",
        "inspect",
        *references,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "null",
        "[1, 2]",
        "[]",
        '[{"Id": "one"}]',
        '[{"Id": "one"}, {"Id": "one"}, {"Id": "one"}]',
        '[{"Id": "one"}, {"Id": 2}]',
        '[{"Id": "one"}, {"Id": ""}]',
        '[{"Id": "one"}, {}]',
        '[{"Id": "one"}, {"Id": "two"}]',
        '[{"Id": "one"}, {"Id": "one", "Enabled": true}]',
    ],
)
def test_batch_inspect_validates_all_plugins_before_hydration(payload: str) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ) as command,
        patch.object(Plugin, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.plugin.inspect(["alias", "alias"])
    command.assert_called_once()
    hydrate.assert_not_called()


def test_batch_inspect_rejects_plugin_full_id_mismatch_before_hydration() -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    payload = json.dumps([{"Id": "a" * 64}, {"Id": "c" * 64}]).encode()
    with (
        patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")),
        patch.object(Plugin, "_set_inspect_result") as hydrate,
        pytest.raises(ParsingError),
    ):
        client.plugin.inspect(["a" * 64, "b" * 64])
    hydrate.assert_not_called()


def test_batch_inspect_empty_plugins_avoids_lookup() -> None:
    with patch("shutil.which") as lookup, patch("subprocess.run") as command:
        assert DockerClient().plugin.inspect([]) == []
    lookup.assert_not_called()
    command.assert_not_called()


@pytest.mark.parametrize("handle", [False, True])
@pytest.mark.parametrize("timeout", [None, 2.5])
def test_plugin_push_forwards_timeout(handle: bool, timeout: float | None) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch("pywhales.components.plugin.cli_wrapper.run") as mock_run:
        mock_run.return_value = ""

        if handle:
            Plugin(client.client_config, test_plugin_name, is_immutable_id=True).push(
                timeout=timeout
            )
        else:
            client.plugin.push(test_plugin_name, timeout=timeout)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["timeout"] == timeout


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), True])
def test_plugin_push_invalid_timeout_precedes_launch(timeout: float) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with (
        patch("pywhales.components.plugin.cli_wrapper.run") as mock_run,
        pytest.raises(ValueError),
    ):
        client.plugin.push(test_plugin_name, timeout=timeout)

    mock_run.assert_not_called()


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
def test_plugin_remove_empty_selection_skips_cli(
    make_empty: Callable[[], Iterable[str]],
) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.plugin.remove(make_empty(), force=True)


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_plugin_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.plugin.remove(make_selection(), force=True)
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "plugin",
        "remove",
        "--force",
        "alpha",
    ]


NOT_A_MANAGER = (
    b"Error response from daemon: This node is not a swarm manager. "
    b'Use "docker swarm init" or "docker swarm join" to connect this node to swarm '
    b"and try again."
)
PLUGIN_MISSING = b'Error response from daemon: plugin "resource" not found'
PLUGIN_JSON = b'[{"Id":"resource"}]'
# Each failure below is not this plugin's own not-found error.
OTHER_FAILURES = [
    pytest.param(b"permission denied", DockerException, id="command-failure"),
    pytest.param(NOT_A_MANAGER, NotASwarmManager, id="not-a-swarm-manager"),
    pytest.param(
        b'Error response from daemon: plugin "resource" is enabled',
        DockerException,
        id="plugin-enabled",
    ),
    pytest.param(
        b"Error response from daemon: No such container: resource",
        NoSuchContainer,
        id="container-not-found",
    ),
]


def _plugin() -> tuple[DockerClient, Plugin]:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    return client, Plugin(client.client_config, "resource", is_immutable_id=True)


@pytest.mark.parametrize("missing", [False, True], ids=["removed", "already-missing"])
def test_plugin_cleanup_ignores_only_a_missing_plugin(missing: bool) -> None:
    _, plugin = _plugin()
    response = CompletedProcess(
        [], 1 if missing else 0, b"", PLUGIN_MISSING if missing else b""
    )
    with patch("subprocess.run", return_value=response) as command, plugin:
        pass
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "plugin",
        "remove",
        "--force",
        "resource",
    ]


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_plugin_cleanup_propagates_other_removal_failures(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, plugin = _plugin()
    response = CompletedProcess([], 17, b"output", stderr)
    with (
        patch("subprocess.run", return_value=response) as command,
        pytest.raises(DockerException) as raised,
        plugin,
    ):
        pass
    command.assert_called_once()
    assert type(raised.value) is error_type
    assert raised.value.return_code == 17
    assert raised.value.stderr == stderr.decode()


@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_plugin_cleanup_failure_joins_the_body_error(
    stderr: bytes, error_type: type[DockerException]
) -> None:
    _, plugin = _plugin()
    body = RuntimeError("body")
    response = CompletedProcess([], 17, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(BaseExceptionGroup) as raised,
        plugin,
    ):
        raise body
    assert raised.value.message == "Plugin body and cleanup both failed"
    assert raised.value.exceptions[0] is body
    assert type(raised.value.exceptions[1]) is error_type


def test_plugin_missing_at_cleanup_leaves_the_body_error_alone() -> None:
    _, plugin = _plugin()
    body = RuntimeError("body")
    response = CompletedProcess([], 1, b"", PLUGIN_MISSING)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(RuntimeError) as raised,
        plugin,
    ):
        raise body
    assert raised.value is body


def _exists(client: DockerClient, plugin: Plugin, *, as_handle: bool) -> bool:
    return plugin.exists() if as_handle else client.plugin.exists("resource")


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_plugin_exists_is_true_after_inspect(as_handle: bool) -> None:
    client, plugin = _plugin()
    response = CompletedProcess([], 0, PLUGIN_JSON, b"")
    with patch("subprocess.run", return_value=response) as command:
        found = _exists(client, plugin, as_handle=as_handle)
    assert found is True
    command.assert_called_once()
    assert command.call_args.args[0] == [
        "docker-test",
        "plugin",
        "inspect",
        "resource",
    ]


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
def test_plugin_exists_is_false_for_a_missing_plugin(as_handle: bool) -> None:
    client, plugin = _plugin()
    response = CompletedProcess([], 1, b"", PLUGIN_MISSING)
    with patch("subprocess.run", return_value=response):
        found = _exists(client, plugin, as_handle=as_handle)
    assert found is False


@pytest.mark.parametrize("as_handle", [False, True], ids=["cli", "handle"])
@pytest.mark.parametrize(("stderr", "error_type"), OTHER_FAILURES)
def test_plugin_exists_propagates_other_failures(
    as_handle: bool, stderr: bytes, error_type: type[DockerException]
) -> None:
    client, plugin = _plugin()
    response = CompletedProcess([], 1, b"", stderr)
    with (
        patch("subprocess.run", return_value=response),
        pytest.raises(DockerException) as raised,
    ):
        _exists(client, plugin, as_handle=as_handle)
    assert type(raised.value) is error_type


def test_plugin_removed_inside_its_context_exits_cleanly(
    docker_client: DockerClient,
) -> None:
    with docker_client.plugin.install(test_plugin_name) as my_plugin:
        assert my_plugin.exists()
        assert docker_client.plugin.exists(my_plugin.id)
        my_plugin.remove(force=True)
        assert not my_plugin.exists()
    assert not my_plugin.exists()
    assert not docker_client.plugin.exists(test_plugin_name)
