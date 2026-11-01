import json
import traceback
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from pydantic import JsonValue, ValidationError

from pywhales import (
    Config,
    Container,
    Context,
    DockerClient,
    Image,
    Network,
    Node,
    Plugin,
    Secret,
    Service,
    Task,
    Volume,
)
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.exceptions import NoSuchContainer

type ResourceType = type[
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
]

SCALAR_RESOURCES = [
    Config,
    Container,
    Context,
    Image,
    Network,
    Node,
    Plugin,
    Secret,
    Service,
    Task,
    Volume,
]


@pytest.mark.parametrize("resource_type", SCALAR_RESOURCES)
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '[{"sentinel": "scalar-secret"}, {}]',
        '["scalar-secret"]',
        '{"sentinel": "scalar-secret"}',
        '[{"sentinel": "scalar-secret"}',
    ],
    ids=["empty", "multiple", "non-object", "non-array", "invalid-json"],
)
def test_scalar_inspect_rejects_malformed_response(
    resource_type: ResourceType, raw: bool, payload: str, tmp_path: Path
) -> None:
    config = ClientConfig(raw_diagnostics=raw, _client_call_with_path=["mock-cli"])
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, payload.encode(), b""),
        ),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ParsingError) as raised,
    ):
        resource_type(config, "reference")

    if not raw:
        assert "scalar-secret" not in "".join(traceback.format_exception(raised.value))
        assert raised.value.__cause__ is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("resource_type", SCALAR_RESOURCES)
@pytest.mark.parametrize("raw", [False, True])
def test_scalar_reload_rejects_changed_identity_without_publishing(
    resource_type: ResourceType, raw: bool, tmp_path: Path
) -> None:
    config = ClientConfig(raw_diagnostics=raw, _client_call_with_path=["mock-cli"])
    original = {"Id": "original", "ID": "original", "Name": "original"}
    changed = {"Id": "scalar-secret", "ID": "scalar-secret", "Name": "scalar-secret"}
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, json.dumps([original]).encode(), b""),
    ):
        resource = resource_type(config, "reference")
    snapshot, timestamp = resource._inspect_result, resource._last_refreshed_time
    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 0, json.dumps([changed]).encode(), b""),
        ),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ParsingError) as raised,
    ):
        resource.reload()

    assert resource._inspect_result is snapshot
    assert resource._last_refreshed_time == timestamp
    if not raw:
        assert "scalar-secret" not in "".join(traceback.format_exception(raised.value))
        assert raised.value.__cause__ is None
    assert list(tmp_path.iterdir()) == []


def test_current_context_reload_accepts_changed_name() -> None:
    config = ClientConfig(_client_call_with_path=["mock-cli"])
    context = Context(config, None, is_immutable_id=True)
    with patch(
        "subprocess.run",
        return_value=CompletedProcess([], 0, b'[{"Name":"current"}]', b""),
    ):
        context.reload()
    assert context._inspect_result is not None
    assert context._inspect_result.name == "current"


@pytest.mark.parametrize("resource_type", SCALAR_RESOURCES)
def test_scalar_reload_accepts_matching_full_identity(
    resource_type: ResourceType,
) -> None:
    identity = "a" * 64
    payload = json.dumps([{"Id": identity, "ID": identity, "Name": identity}]).encode()
    config = ClientConfig(_client_call_with_path=["mock-cli"])
    resource = resource_type(config, identity, is_immutable_id=True)
    with patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")):
        resource.reload()
    assert resource._inspect_result is not None
    assert getattr(resource._inspect_result, resource._id_in_inspect) == identity


@pytest.mark.parametrize("component", ["container", "image", "network", "volume"])
@pytest.mark.parametrize("raw", [False, True])
def test_inspect_mismatched_reference_renders_values_only_in_raw_mode(
    component: str, raw: bool, tmp_path: Path
) -> None:
    secret = "reference-diagnostic-sentinel"
    client = DockerClient(raw_diagnostics=raw)
    client.client_config._client_call_with_path = ["mock-cli"]
    api = {
        "container": client.container,
        "image": client.image,
        "network": client.network,
        "volume": client.volume,
    }[component]
    payload = json.dumps([{"Id": "unrelated", "Name": "unrelated"}]).encode()

    with (
        patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ParsingError) as raised,
    ):
        api.inspect([secret])

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("component", ["config", "node", "secret", "service"])
@pytest.mark.parametrize("conflicting", [False, True], ids=["reference", "snapshot"])
@pytest.mark.parametrize("raw", [False, True])
def test_swarm_inspect_renders_reference_and_snapshot_values_only_in_raw_mode(
    component: str,
    conflicting: bool,
    raw: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "swarm-diagnostic-sentinel"
    client = DockerClient(raw_diagnostics=raw)
    client.client_config._client_call_with_path = ["mock-cli"]
    api = {
        "config": client.config,
        "node": client.node,
        "secret": client.secret,
        "service": client.service,
    }[component]
    snapshots: list[dict[str, JsonValue]] = [{"ID": "unrelated"}]
    references = [secret]
    if conflicting:
        snapshots = [{"ID": secret, "Version": {"Index": index}} for index in (1, 2)]
        references = [secret, secret]
    payload = json.dumps(snapshots).encode()

    with (
        patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ParsingError) as raised,
    ):
        api.inspect(references)

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw
    if not raw:
        assert secret not in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


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
@pytest.mark.parametrize("raw", [False, True])
def test_scalar_inspect_fetch_validation_is_safe_without_changing_raw_errors(
    resource_type: ResourceType, raw: bool, tmp_path: Path
) -> None:
    secret = "scalar-fetch-diagnostic-sentinel"
    nested: JsonValue = secret
    for _ in range(300):
        nested = [nested]
    snapshot = {secret: nested}
    payload = json.dumps(
        snapshot if resource_type is ManifestList else [snapshot]
    ).encode()
    config = ClientConfig(raw_diagnostics=raw, _client_call_with_path=["mock-cli"])

    with (
        patch("subprocess.run", return_value=CompletedProcess([], 0, payload, b"")),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(ValidationError if raw else ParsingError) as raised,
    ):
        resource_type(config, "reference")

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw
    assert list(tmp_path.iterdir()) == []


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
@pytest.mark.parametrize("raw", [False, True])
def test_scalar_inspect_command_failures_keep_classification_and_raw_attributes(
    resource_type: ResourceType,
    raw: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "execution-diagnostic-sentinel"
    stderr = f"No such container: {secret}"
    config = ClientConfig(raw_diagnostics=raw, _client_call_with_path=["mock-cli"])

    with (
        patch(
            "subprocess.run",
            return_value=CompletedProcess([], 7, secret.encode(), stderr.encode()),
        ),
        patch("pywhales.client_config.tempfile.tempdir", str(tmp_path)),
        pytest.raises(NoSuchContainer) as raised,
    ):
        resource_type(config, secret)

    assert (secret in "".join(traceback.format_exception(raised.value))) is raw
    assert raised.value.return_code == 7
    assert raised.value.stdout == secret
    assert raised.value.stderr == stderr
    assert raised.value.docker_command[-1] == secret
    if not raw:
        assert secret not in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
