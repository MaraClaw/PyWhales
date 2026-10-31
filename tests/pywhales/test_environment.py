import json
import logging
import os
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from pathlib import Path
from threading import Barrier
from typing import override
from unittest.mock import patch

import pytest

import pywhales._environment as environment_module
from pywhales import DockerClient
from pywhales._environment import (
    finalize_environment,
    frozen_environment_scope,
    resolve_environment,
)
from pywhales._execution_policy import get_execution_policy
from pywhales.admission import EndpointIdentity
from pywhales.client_config import ClientConfig, Command
from pywhales.profiles import EndpointBinding, PolicyError
from pywhales.telemetry import TelemetryRecord
from pywhales.utils import run, stream_stdout_and_stderr


def test_client_owns_overlay_and_supplied_config_wins() -> None:
    original = {"TEST_ENV_SECRET": "secret-sentinel"}
    with patch("shutil.which") as lookup:
        client = DockerClient(environment=original)
        selected = DockerClient(
            client_config=client.client_config, environment={"": "bad"}
        )
    original["TEST_ENV_SECRET"] = "changed"
    assert selected.client_config is client.client_config
    assert client.client_config.environment == {"TEST_ENV_SECRET": "secret-sentinel"}
    assert "secret-sentinel" not in repr(client.client_config)
    lookup.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_launch_snapshots_all_layers_before_stringifying(
    stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ENV_INHERITED", "before")
    operation = {"TEST_ENV_OPERATION": "before"}
    config = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "before"}
    )

    class MutatingArgument:
        @override
        def __str__(self) -> str:
            monkeypatch.setenv("TEST_ENV_INHERITED", "after")
            operation["TEST_ENV_OPERATION"] = "after"
            assert isinstance(config.environment, dict)
            config.environment["TEST_ENV_CLIENT"] = "after"
            return "import os; print('|'.join(os.environ[k] for k in ('TEST_ENV_INHERITED','TEST_ENV_CLIENT','TEST_ENV_OPERATION')))"

    command = [*config.docker_cmd, "-c", MutatingArgument()]
    if stream:
        output = (
            b"".join(
                data
                for source, data in stream_stdout_and_stderr(command, env=operation)
                if source == "stdout"
            )
            .decode()
            .strip()
        )
    else:
        output = run(command, env=operation)
    assert output == "before|before|before"


@pytest.mark.parametrize("composition", ["list", "copy", "deepcopy", "command"])
def test_concurrent_clients_preserve_overlay_through_command_copies(
    composition: str,
) -> None:
    barrier = Barrier(2)

    def execute(value: str) -> str:
        config = ClientConfig(
            client_call=[sys.executable], environment={"TEST_ENV_CLIENT": value}
        )
        command = [
            *config.docker_cmd,
            "-c",
            "import os; print(os.environ['TEST_ENV_CLIENT'])",
        ]
        selected = {
            "list": command[:],
            "copy": copy(command),
            "deepcopy": deepcopy(command),
            "command": Command(command),
        }[composition]
        barrier.wait(timeout=3)
        return run(selected)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(execute, ["first", "second"])) == ["first", "second"]
    assert os.environ.get("TEST_ENV_CLIENT") not in {"first", "second"}


@pytest.mark.parametrize(
    "entry",
    [
        {"": "secret"},
        {"secret=key": "value"},
        {"secret\x00": "value"},
        {"key": "secret\x00"},
        {1: "secret"},
        {"secret": 1},
    ],
)
def test_invalid_overlay_rejected_without_disclosing_values(
    entry: Mapping[str, str],
) -> None:
    with pytest.raises((TypeError, ValueError)) as error:
        ClientConfig(environment=entry)
    assert "secret" not in str(error.value)


def test_later_mutation_is_validated_on_each_launch() -> None:
    config = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "first"}
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import os; print(os.environ['TEST_ENV_CLIENT'])",
    ]
    policy = get_execution_policy(command)
    assert isinstance(config.environment, dict)
    config.environment["TEST_ENV_CLIENT"] = "second"
    assert policy.environment["TEST_ENV_CLIENT"] == "first"
    assert run(command) == "second"
    config.environment[""] = "secret"
    with pytest.raises(ValueError):
        run(command)


def test_frozen_scope_matches_identity_and_is_consumed_before_nested_launch() -> None:
    config = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "planned"}
    )
    other = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "other"}
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import os; print(os.environ['TEST_ENV_CLIENT'])",
    ]
    other_command = [
        *other.docker_cmd,
        "-c",
        "import os; print(os.environ['TEST_ENV_CLIENT'])",
    ]
    policy = get_execution_policy(command)
    planned = resolve_environment(policy.environment)
    assert isinstance(config.environment, dict)
    config.environment[""] = "invalid"
    with frozen_environment_scope(config, planned):
        assert run(other_command) == "other"
        assert run(command) == "planned"
        with pytest.raises(ValueError):
            run(command)


def test_observer_nested_launch_uses_fresh_environment() -> None:
    config = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "planned"}
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import os; print(os.environ['TEST_ENV_CLIENT'])",
    ]
    policy = get_execution_policy(command)
    planned = resolve_environment(policy.environment)
    config.environment = {"TEST_ENV_CLIENT": "fresh"}
    observed: list[str] = []

    def observer(record: TelemetryRecord) -> None:
        if record.phase == "launch":
            config.telemetry_observer = None
            observed.append(run(command))

    config.telemetry_observer = observer
    with frozen_environment_scope(config, planned):
        result = run(command)
    assert result == "planned"
    assert observed == ["fresh"]


def test_windows_layers_replace_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_module, "_WINDOWS", True)
    monkeypatch.setattr(
        environment_module.os,
        "environ",
        {"Path": "ambient", "docker_cli_experimental": "old"},
    )
    merged = resolve_environment({"PATH": "client"}, {"path": "operation"})
    result = finalize_environment(merged, ["docker", "buildx"])
    assert result == {"path": "operation", "DOCKER_CLI_EXPERIMENTAL": "enabled"}
    with pytest.raises(ValueError):
        environment_module.copy_environment({"PATH": "secret", "path": "other"})


@pytest.mark.parametrize("stream", [False, True])
def test_buildx_mandatory_environment_in_real_children(
    stream: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "buildx").write_text(
        "import os\nprint(os.environ['DOCKER_CLI_EXPERIMENTAL'])\n"
    )
    monkeypatch.chdir(tmp_path)
    config = ClientConfig(
        client_call=[sys.executable], environment={"DOCKER_CLI_EXPERIMENTAL": "client"}
    )
    command = [*config.docker_cmd, "buildx"]
    if stream:
        result = list(
            stream_stdout_and_stderr(
                command, env={"DOCKER_CLI_EXPERIMENTAL": "operation"}
            )
        )
        assert result == [("stdout", b"enabled\n")]
    else:
        assert run(command, env={"DOCKER_CLI_EXPERIMENTAL": "operation"}) == "enabled"


def test_overlay_change_invalidates_endpoint_binding() -> None:
    client = DockerClient(environment={"TEST_ENV_CLIENT": "first"})
    binding = EndpointBinding(EndpointIdentity("test"), client.client_config)
    assert isinstance(client.client_config.environment, dict)
    client.client_config.environment["TEST_ENV_CLIENT"] = "second"
    with pytest.raises(PolicyError):
        binding.validate(client)


@pytest.mark.parametrize("stream", [False, True])
def test_compose_build_values_are_visible_to_project_environment(
    stream: bool, tmp_path: Path
) -> None:
    driver = tmp_path / "compose.py"
    record = tmp_path / "project.json"
    driver.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from string import Template\n"
        "project = {'image': Template('${APP_BUILD_VALUE}:latest').substitute(os.environ), "
        "'environment': {name: os.environ[name] for name in ['APP_BUILD_VALUE']}}\n"
        "Path(sys.argv[1]).write_text(json.dumps(project))\n"
    )
    client = DockerClient(
        client_call=[sys.executable, str(driver), str(record)],
        strict_env_transport=True,
    )
    result = client.compose.build(
        build_args={"APP_BUILD_VALUE": "build-input"}, stream_logs=stream
    )
    if result is not None:
        with result:
            list(result)
    assert json.loads(record.read_text()) == {
        "image": "build-input:latest",
        "environment": {"APP_BUILD_VALUE": "build-input"},
    }


def test_safe_diagnostics_hide_client_overlay(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PYWHALES_DEBUG", "1")
    caplog.set_level(logging.DEBUG)
    client = DockerClient(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "secret-sentinel"}
    )
    output = run(
        [
            *client.client_config.docker_cmd,
            "-c",
            "import os; print(os.environ['TEST_ENV_CLIENT'])",
        ]
    )
    assert output == "secret-sentinel"
    assert "secret-sentinel" not in caplog.text + capsys.readouterr().out


def test_overlay_path_does_not_change_ambient_lookup_or_cached_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path(sys.executable)
    monkeypatch.setenv("PATH", str(executable.parent))
    config = ClientConfig(
        client_call=[executable.name, "-c", "import os; print(os.environ['PATH'])"],
        environment={"PATH": "child-only"},
    )
    command = config.docker_cmd
    monkeypatch.setenv("PATH", "changed-after-lookup")
    assert run(command) == "child-only"
    assert config.get_client_call_with_path()[0] == executable


@pytest.mark.parametrize("stream", [False, True])
def test_operation_overrides_client_and_ambient(
    stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ENV_CLIENT", "ambient")
    config = ClientConfig(
        client_call=[sys.executable], environment={"TEST_ENV_CLIENT": "client"}
    )
    command = [
        *config.docker_cmd,
        "-c",
        "import os; print(os.environ['TEST_ENV_CLIENT'])",
    ]
    if stream:
        assert list(
            stream_stdout_and_stderr(command, env={"TEST_ENV_CLIENT": "operation"})
        ) == [("stdout", b"operation\n")]
    else:
        assert run(command, env={"TEST_ENV_CLIENT": "operation"}) == "operation"
