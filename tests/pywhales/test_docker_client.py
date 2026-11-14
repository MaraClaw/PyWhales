import json
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch
from uuid import uuid4

import pytest

from pywhales import DockerClient, Image
from pywhales.client_config import ClientConfig
from pywhales.components.container.models import PortBinding
from pywhales.components.top import ContainerTopResult
from pywhales.exceptions import DockerException


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "8", b"8"])
def test_general_stream_budget_rejects_invalid_client_option_before_lookup(
    budget: int | None,
) -> None:
    with patch("shutil.which") as lookup, pytest.raises(ValueError):
        DockerClient(stream_buffer_bytes=budget)

    lookup.assert_not_called()


def test_execution_policy_uses_supplied_config_without_constructor_overrides() -> None:
    config = ClientConfig(stream_buffer_bytes=17, raw_diagnostics=True)

    client = DockerClient(
        client_config=config, stream_buffer_bytes=0, raw_diagnostics=False
    )

    assert client.client_config is config
    assert client.client_config.stream_buffer_bytes == 17
    assert client.client_config.raw_diagnostics is True


def test_execution_policy_is_isolated_when_clients_are_constructed() -> None:
    safe = DockerClient(stream_buffer_bytes=31)
    raw = DockerClient(stream_buffer_bytes=17, raw_diagnostics=True)
    default = DockerClient()

    safe.client_config.stream_buffer_bytes = 23

    assert raw.client_config.stream_buffer_bytes == 17
    assert raw.client_config.raw_diagnostics is True
    assert safe.client_config.raw_diagnostics is False
    assert default.client_config.stream_buffer_bytes is None
    assert default.client_config.raw_diagnostics is False


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "8", b"8"])
def test_image_stream_budget_rejects_invalid_client_option_before_lookup(
    budget: int | None,
) -> None:
    with patch("shutil.which") as lookup, pytest.raises(ValueError):
        DockerClient(image_stream_buffer_bytes=budget)

    lookup.assert_not_called()


@pytest.mark.parametrize("budget", [None, 17])
@pytest.mark.parametrize("ignored_budget", [0, 31])
def test_image_stream_budget_keeps_supplied_config_precedence(
    budget: int | None, ignored_budget: int
) -> None:
    config = ClientConfig(image_stream_buffer_bytes=budget, host="selected")

    client = DockerClient(
        client_config=config, image_stream_buffer_bytes=ignored_budget, host="ignored"
    )

    assert client.client_config is config
    assert client.client_config.image_stream_buffer_bytes == budget
    assert client.client_config.host == "selected"


def test_image_stream_budget_keeps_clients_and_mutable_options_isolated() -> None:
    files = [Path("compose.yml")]
    call = [sys.executable, "engine"]
    first = DockerClient(
        image_stream_buffer_bytes=17, compose_files=files, client_call=call
    )
    second = DockerClient(
        image_stream_buffer_bytes=31, compose_files=files, client_call=call
    )
    default = DockerClient()

    first.client_config.compose_files.append(Path("extra.yml"))
    first.client_config.client_call.append("--debug")
    first.client_config.image_stream_buffer_bytes = 23

    assert second.client_config.image_stream_buffer_bytes == 31
    assert default.client_config.image_stream_buffer_bytes is None
    assert second.client_config.compose_files == files == [Path("compose.yml")]
    assert second.client_config.client_call == call == [sys.executable, "engine"]
    assert default.client_config.compose_files == []
    assert default.client_config.client_call == ["docker"]


@patch(
    "pywhales.components.container.cli_wrapper.run", return_value="PID CMD\n1 sleep 60"
)
def test_top_dispatches_process_listing_when_root_alias_is_used(run_mock: Mock) -> None:
    client = DockerClient(client_call=[sys.executable, "engine"], context="remote")

    result = client.top("workload")

    assert isinstance(result, ContainerTopResult)
    assert result.rows == (("1", "sleep 60"),)
    run_mock.assert_called_once_with(
        [
            sys.executable,
            "engine",
            "--context",
            "remote",
            "container",
            "top",
            "workload",
        ]
    )


@patch("pywhales.components.container.cli_wrapper.run", return_value="")
def test_stop_dispatch_is_unchanged_when_root_and_component_are_used(
    run_mock: Mock,
) -> None:
    client = DockerClient(client_call=[sys.executable, "engine"])

    client.stop("first", time=0)
    client.container.stop("second", time=1)

    assert run_mock.call_args_list == [
        call([sys.executable, "engine", "container", "stop", "--timeout", 0, "first"]),
        call([sys.executable, "engine", "container", "stop", "--timeout", 1, "second"]),
    ]


@pytest.mark.parametrize("component_stop", [False, True])
def test_top_leaves_running_then_stop_stops_when_inspecting_live_container(
    docker_client: DockerClient, component_stop: bool
) -> None:
    container = docker_client.run(
        "python:3.14.10-slim-trixie", ["sleep", "300"], detach=True
    )
    try:
        result = docker_client.top(container)

        assert result.rows
        assert any("sleep 300" in row[-1] for row in result.rows)
        container.reload()
        assert container.state is not None
        assert container.state.running is True
        stop = docker_client.container.stop if component_stop else docker_client.stop
        stop(container, time=0)
        container.reload()
        assert container.state is not None
        assert container.state.running is False
    finally:
        docker_client.remove(container, force=True)


def test_version(ctr_client: DockerClient) -> None:
    version_info = ctr_client.version()
    assert version_info.client is not None
    assert version_info.client.version is not None
    assert re.match(r"\d+\.\d+", version_info.client.version)
    assert version_info.server is not None
    assert version_info.server.version is not None
    assert re.match(r"\d+\.\d+", version_info.server.version)


def _isolated_auth_client(
    docker_client: DockerClient, config_dir: Path, registry: str
) -> DockerClient:
    """Create separate auth storage while retaining the selected Docker context.

    Inspect the source context, link its disk-backed metadata when present, and
    write a fresh config.json without copied credentials. In-memory contexts
    need no metadata symlink; the remaining client configuration is retained.

    Args:
        docker_client: Source client used for context inspection and options.
        config_dir: Existing writable directory for new local auth configuration.
        registry: Registry key initialized with empty authentication settings.

    Returns:
        A client using config_dir; the caller owns that directory's cleanup.
    """
    current_context = docker_client.context.inspect()
    storage = current_context.storage
    assert storage is not None
    assert storage.metadata_path is not None
    if storage.metadata_path != "<IN MEMORY>":
        (config_dir / "contexts").symlink_to(
            Path(storage.metadata_path).parent.parent, target_is_directory=True
        )
    (config_dir / "config.json").write_text(
        json.dumps({"currentContext": current_context.name, "auths": {registry: {}}})
    )
    return DockerClient(
        client_config=replace(docker_client.client_config, config=config_dir)
    )


@patch("pywhales.components.context.cli_wrapper.run")
def test_auth_config_preserves_selected_context_without_credentials(
    run_mock: Mock, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    metadata_path = source / "contexts" / "meta" / "context-id"
    metadata_path.mkdir(parents=True)
    source_config = json.dumps(
        {"currentContext": "remote", "auths": {"private.invalid": {"auth": "secret"}}}
    )
    (source / "config.json").write_text(source_config)
    run_mock.return_value = json.dumps(
        [{"Name": "remote", "Storage": {"MetadataPath": str(metadata_path)}}]
    )
    client = DockerClient(config=source, client_call=[sys.executable, "engine"])
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    auth_client = _isolated_auth_client(client, auth_dir, "registry.invalid")

    assert json.loads((auth_dir / "config.json").read_text()) == {
        "currentContext": "remote",
        "auths": {"registry.invalid": {}},
    }
    assert (auth_dir / "contexts").resolve() == source / "contexts"
    assert auth_client.client_config.config == auth_dir
    assert client.client_config.config == source
    assert (source / "config.json").read_text() == source_config


@patch("pywhales.components.context.cli_wrapper.run")
def test_auth_config_preserves_explicit_endpoint_options_when_context_is_in_memory(
    run_mock: Mock, tmp_path: Path
) -> None:
    run_mock.return_value = json.dumps(
        [{"Name": "default", "Storage": {"MetadataPath": "<IN MEMORY>"}}]
    )
    client = DockerClient(
        config=tmp_path / "source",
        client_call=[sys.executable, "engine"],
        host="tcp://remote.invalid:2376",
        tls=True,
        tlsverify=True,
        tlscacert=tmp_path / "ca.pem",
        tlscert=tmp_path / "cert.pem",
        tlskey=tmp_path / "key.pem",
    )
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    auth_client = _isolated_auth_client(client, auth_dir, "registry.invalid")

    assert auth_client.client_config == replace(client.client_config, config=auth_dir)
    assert client.client_config.config == tmp_path / "source"
    assert not (auth_dir / "contexts").exists()


def test_login_logout(
    docker_client: DockerClient, docker_registry_without_login: str, tmp_path: Path
) -> None:
    auth_client = _isolated_auth_client(
        docker_client, tmp_path, docker_registry_without_login
    )
    config_path = tmp_path / "config.json"
    busybox_image = docker_client.pull("busybox")
    assert isinstance(busybox_image, Image)
    busybox_image.tag(f"{docker_registry_without_login}/my_busybox")
    with pytest.raises(DockerException):
        auth_client.push(f"{docker_registry_without_login}/my_busybox")
    try:
        auth_client.login(
            docker_registry_without_login, username="my_user", password="my_password"
        )
        assert docker_registry_without_login in config_path.read_text()
        auth_client.push(f"{docker_registry_without_login}/my_busybox")
        auth_client.push(
            [f"{docker_registry_without_login}/my_busybox" for _ in range(2)]
        )
        auth_client.pull(f"{docker_registry_without_login}/my_busybox")
    finally:
        auth_client.logout(docker_registry_without_login)
    assert docker_registry_without_login not in config_path.read_text()


def test_docker_client_options(docker_client: DockerClient) -> None:
    engine = docker_client.container.create(
        "docker:29-dind",
        ["dockerd", "--host=tcp://0.0.0.0:2375", "--tls=false"],
        envs={"DOCKER_TLS_CERTDIR": ""},
        privileged=True,
        publish=[("127.0.0.1::2375",)],
        name=f"pywhales-options-{uuid4().hex}",
    )
    try:
        engine.start()
        network_settings = engine.network_settings
        assert network_settings is not None
        assert network_settings.ports is not None
        bindings = network_settings.ports["2375/tcp"]
        assert isinstance(bindings, list)
        binding = PortBinding.model_validate(bindings[0])
        assert binding.host_ip == "127.0.0.1"
        assert binding.host_port is not None
        endpoint = f"tcp://127.0.0.1:{binding.host_port}"
        client_call = docker_client.client_config.client_call
        dind_client = DockerClient(host=endpoint, client_call=client_call)
        deadline = time.monotonic() + 60
        while True:
            try:
                subprocess.run(
                    [*(str(arg) for arg in dind_client.docker_cmd), "version"],
                    capture_output=True,
                    check=True,
                    timeout=2,
                )
            except subprocess.CalledProcessError, subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
            else:
                break
        version_info = dind_client.version()
        assert version_info.server is not None
        assert version_info.server.version is not None
        assert version_info.server.version.startswith("29.")
        assert dind_client.image.list() == []
        caller_client = DockerClient(client_call=[*client_call, f"--host={endpoint}"])
        assert caller_client.image.list() == []
        assert "Hello from Docker!" in dind_client.run("hello-world", remove=True)
        assert "Hello from Docker!" in caller_client.run("hello-world", remove=True)
    finally:
        engine.remove(force=True, volumes=True)
