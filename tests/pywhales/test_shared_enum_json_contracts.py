import json
from functools import partial
from pathlib import Path
from tempfile import mkstemp
from unittest.mock import Mock

import pytest
from pydantic import JsonValue, ValidationError

from pywhales import DockerClient, enums
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components.config.cli_wrapper import Config
from pywhales.components.container.cli_wrapper import Container
from pywhales.components.context.cli_wrapper import Context
from pywhales.components.image.cli_wrapper import Image
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.components.network.cli_wrapper import Network
from pywhales.components.node.cli_wrapper import Node
from pywhales.components.plugin.cli_wrapper import Plugin
from pywhales.components.secret.cli_wrapper import Secret
from pywhales.components.service.cli_wrapper import Service
from pywhales.components.task.cli_wrapper import Task
from pywhales.components.volume.cli_wrapper import Volume


@pytest.mark.parametrize(
    ("client_type", "progress"),
    [
        (enums.ClientType.DOCKER, enums.ComposeProgress.JSON),
        ("docker", "json"),
    ],
)
def test_enum_options_reach_compose_command_when_client_is_constructed(
    client_type: enums.ClientTypeInput, progress: enums.ComposeProgressInput
) -> None:
    client = DockerClient(client_type=client_type, compose_progress=progress)
    client.client_config._client_call_with_path = ["docker-test"]

    command = client.client_config.docker_compose_cmd

    assert [str(argument) for argument in command] == [
        "docker-test",
        "compose",
        "--progress",
        "json",
    ]
    assert client.client_config.client_type == "docker"


def test_config_remains_mutable_when_constructed_with_positional_context() -> None:
    config = ClientConfig(None, "before")
    config._client_call_with_path = ["docker-test"]

    config.context = "after"

    assert list(config.docker_cmd) == ["docker-test", "--context", "after"]
    assert config.client_type == "unknown"
    assert config.compose_progress is None


def test_shared_json_parser_preserves_snapshot_when_cache_is_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pywhales.client_config.CACHE_VALIDITY_PERIOD", 3600)
    network = Network(ClientConfig(), "network-id", is_immutable_id=True)
    payload: dict[str, JsonValue] = {
        "Id": "network-id",
        "Scope": "future-scope",
        "Options": {"extension": [None, True, 1, 2.5, {"nested": []}]},
    }

    snapshot = network._parse_inspect_result(payload)
    network._set_inspect_result(snapshot)

    assert network._get_inspect_result() is snapshot
    assert network.scope == "future-scope"
    assert network.options == payload["Options"]


def test_shared_json_parser_keeps_validation_cause_and_json_report_in_raw_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "pywhales.client_config.tempfile.mkstemp", partial(mkstemp, dir=tmp_path)
    )
    network = Network(
        ClientConfig(raw_diagnostics=True), "network-id", is_immutable_id=True
    )
    payload: dict[str, JsonValue] = {"Driver": ["invalid-driver"]}

    with pytest.raises(ParsingError) as failure:
        network._parse_inspect_result(payload)

    assert isinstance(failure.value.__cause__, ValidationError)
    (report,) = tmp_path.glob("*.json")
    assert json.loads(report.read_text()) == payload


@pytest.mark.parametrize(
    "resource_type",
    [
        Config,
        Container,
        Context,
        Image,
        ManifestList,
        Network,
        Node,
        Plugin,
        Secret,
        Service,
        Task,
        Volume,
    ],
)
@pytest.mark.parametrize("item", [None, [], True, 1, 2.5, "non-object"])
def test_inspect_rejects_json_item_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
    resource_type: type[
        Config
        | Container
        | Context
        | Image
        | ManifestList
        | Network
        | Node
        | Plugin
        | Secret
        | Service
        | Task
        | Volume
    ],
    item: JsonValue,
) -> None:
    config = ClientConfig()
    config._client_call_with_path = ["docker-test"]
    json_str = json.dumps(item if resource_type is ManifestList else [item])
    monkeypatch.setattr(f"{resource_type.__module__}.run", Mock(return_value=json_str))
    resource = resource_type(config, "resource-id", is_immutable_id=True)

    with pytest.raises(TypeError if resource_type is ManifestList else ParsingError):
        resource._fetch_and_parse_inspect_result("resource-id")


@pytest.mark.parametrize(
    "resource_type",
    [
        Config,
        Container,
        Context,
        Image,
        ManifestList,
        Network,
        Node,
        Plugin,
        Secret,
        Service,
        Task,
        Volume,
    ],
)
def test_inspect_fetch_preserves_nested_values_when_json_item_is_an_object(
    monkeypatch: pytest.MonkeyPatch,
    resource_type: type[
        Config
        | Container
        | Context
        | Image
        | ManifestList
        | Network
        | Node
        | Plugin
        | Secret
        | Service
        | Task
        | Volume
    ],
) -> None:
    config = ClientConfig()
    config._client_call_with_path = ["docker-test"]
    payload: dict[str, JsonValue] = {"extension": [None, True, 1, 2.5, {"nested": []}]}
    json_str = json.dumps(payload if resource_type is ManifestList else [payload])
    monkeypatch.setattr(f"{resource_type.__module__}.run", Mock(return_value=json_str))
    resource = resource_type(config, "resource-id", is_immutable_id=True)

    result: dict[str, JsonValue] = resource._fetch_inspect_result_json("resource-id")

    assert isinstance(result, dict)
    assert result == payload


@pytest.mark.parametrize(
    "resource_type",
    [
        Config,
        Container,
        Context,
        Image,
        ManifestList,
        Network,
        Node,
        Plugin,
        Secret,
        Service,
        Task,
        Volume,
    ],
)
def test_inspect_fetch_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
    resource_type: type[
        Config
        | Container
        | Context
        | Image
        | ManifestList
        | Network
        | Node
        | Plugin
        | Secret
        | Service
        | Task
        | Volume
    ],
) -> None:
    config = ClientConfig()
    config._client_call_with_path = ["docker-test"]
    monkeypatch.setattr(f"{resource_type.__module__}.run", Mock(return_value="{"))
    resource = resource_type(config, "resource-id", is_immutable_id=True)

    with pytest.raises(
        json.JSONDecodeError if resource_type is ManifestList else ParsingError
    ):
        resource._fetch_inspect_result_json("resource-id")
