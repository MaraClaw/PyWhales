import json
import logging
import os
import sys
from collections.abc import Collection, Mapping
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
from pywhales._execution_policy import PolicyExecutable, get_execution_policy
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


def test_client_copies_inherited_names_and_supplied_config_wins() -> None:
    names = ["APP_SELECTED"]
    with patch("shutil.which") as lookup:
        client = DockerClient(inherited_environment_names=names)
        selected = DockerClient(
            client_config=client.client_config,
            inherited_environment_names="invalid=secret",
            inherit_environment=False,
        )
    names.append("APP_EXCLUDED")
    assert selected.client_config is client.client_config
    assert isinstance(client.client_config.inherited_environment_names, frozenset)
    assert client.client_config.inherited_environment_names == frozenset(
        {"APP_SELECTED"}
    )
    assert client.client_config.inherit_environment
    assert client.client_config == ClientConfig(
        inherited_environment_names={"APP_SELECTED"}
    )
    assert client.client_config != ClientConfig(inherited_environment_names=names)
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "names",
    ["secret", b"secret", 7, ["secret", 7], [""], ["secret=value"], ["secret\0name"]],
)
def test_invalid_inherited_names_reject_without_disclosure(
    names: Collection[str],
) -> None:
    with (
        patch("shutil.which") as lookup,
        pytest.raises((TypeError, ValueError)) as error,
    ):
        DockerClient(inherited_environment_names=names)
    assert "secret" not in str(error.value)
    lookup.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("names", [None, (), ("TEST_ENV_INHERITED",)])
def test_launch_snapshots_all_layers_before_stringifying(
    stream: bool, names: Collection[str] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ENV_INHERITED", "before")
    operation = {"TEST_ENV_OPERATION": "before"}
    config = ClientConfig(
        client_call=[sys.executable],
        environment={"TEST_ENV_CLIENT": "before"},
        inherited_environment_names=names,
    )

    class MutatingArgument:
        @override
        def __str__(self) -> str:
            monkeypatch.setenv("TEST_ENV_INHERITED", "after")
            operation["TEST_ENV_OPERATION"] = "after"
            config.inherited_environment_names = ("TEST_ENV_INHERITED",)
            assert isinstance(config.environment, dict)
            config.environment["TEST_ENV_CLIENT"] = "after"
            return "import os; print('|'.join(os.getenv(k, 'missing') for k in ('TEST_ENV_INHERITED','TEST_ENV_CLIENT','TEST_ENV_OPERATION')))"

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
    inherited = "missing" if names == () else "before"
    assert output == f"{inherited}|before|before"


@pytest.mark.parametrize("composition", ["list", "copy", "deepcopy", "command"])
def test_concurrent_clients_preserve_overlay_through_command_copies(
    composition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_FIRST", "ambient-first")
    monkeypatch.setenv("APP_SECOND", "ambient-second")
    barrier = Barrier(2)

    def execute(value: str) -> str:
        config = ClientConfig(
            client_call=[sys.executable],
            environment={"TEST_ENV_CLIENT": value},
            inherited_environment_names={f"APP_{value.upper()}"},
        )
        command = [
            *config.docker_cmd,
            "-c",
            "import os; print('|'.join(os.getenv(k, 'missing') for k in ('TEST_ENV_CLIENT', 'APP_FIRST', 'APP_SECOND')))",
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
        assert list(pool.map(execute, ["first", "second"])) == [
            "first|ambient-first|missing",
            "second|missing|ambient-second",
        ]
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


@pytest.mark.parametrize("names", [None, (), ("APP_SELECTED",)])
def test_frozen_scope_matches_identity_and_is_consumed_before_nested_launch(
    names: Collection[str] | None,
) -> None:
    config = ClientConfig(
        client_call=[sys.executable],
        environment={"TEST_ENV_CLIENT": "planned"},
        inherited_environment_names=names,
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
    planned = resolve_environment(policy.environment, frozen=policy.frozen_environment)
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
@pytest.mark.parametrize("names", [None, (), ("DOCKER_CLI_EXPERIMENTAL",)])
def test_buildx_mandatory_environment_in_real_children(
    stream: bool,
    names: Collection[str] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "buildx").write_text(
        "import os\nprint(os.environ['DOCKER_CLI_EXPERIMENTAL'])\n"
    )
    monkeypatch.chdir(tmp_path)
    config = ClientConfig(
        client_call=[sys.executable],
        environment={"DOCKER_CLI_EXPERIMENTAL": "client"},
        inherited_environment_names=names,
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


def test_inheritance_change_invalidates_endpoint_binding() -> None:
    client = DockerClient()
    binding = EndpointBinding(EndpointIdentity("test"), client.client_config)
    client.client_config.inherit_environment = False
    with pytest.raises(PolicyError):
        binding.validate(client)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("inherit", [False, True])
@pytest.mark.parametrize("names", [None, (), ("DOCKER_CONTEXT", "PATH", "MISSING")])
def test_child_environment_inheritance(
    stream: bool,
    inherit: bool,
    names: Collection[str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ambient-endpoint")
    monkeypatch.setenv("DOCKER_CONTEXT", "ambient-context")
    monkeypatch.setenv("APP_PARENT_TOKEN", "ambient-token")
    supplied = list(names) if names is not None else None
    client = DockerClient(
        client_call=[sys.executable],
        inherit_environment=inherit,
        inherited_environment_names=supplied,
        environment={"DOCKER_HOST": "explicit-endpoint", "APP_CLIENT": "client"},
    )
    if supplied is not None:
        supplied[:] = ["APP_PARENT_TOKEN", "HOME"]
    command = [
        *client.client_config.docker_cmd,
        "-c",
        "import json, os; print(json.dumps([os.getenv('DOCKER_HOST'), "
        "os.getenv('APP_PARENT_TOKEN'), os.getenv('APP_CLIENT'), os.getenv('PATH'), os.getenv('DOCKER_CONTEXT'), os.getenv('HOME')]))",
    ]
    if stream:
        output = b"".join(
            data
            for source, data in stream_stdout_and_stderr(command)
            if source == "stdout"
        ).decode()
    else:
        output = run(command)
    assert json.loads(output) == [
        "explicit-endpoint",
        "ambient-token" if names is None and inherit else None,
        "client",
        os.environ.get("PATH") if names != () else None,
        "ambient-context" if bool(names) or (names is None and inherit) else None,
        os.environ.get("HOME") if names is None else None,
    ]


def test_selected_windows_names_and_overlays_are_case_equivalent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_module, "_WINDOWS", True)
    monkeypatch.setattr(
        environment_module.os,
        "environ",
        {"Path": "ambient", "APP_Value": "selected", "HOME": "excluded"},
    )
    config = ClientConfig(
        inherited_environment_names={"pAtH", "app_value", "APP_VALUE", "MISSING"},
        environment={"PATH": "client"},
    )
    policy = get_execution_policy([PolicyExecutable("docker", config)])
    assert resolve_environment(
        policy.environment, {"path": "operation"}, frozen=policy.frozen_environment
    ) == {"APP_Value": "selected", "path": "operation"}


def test_isolated_windows_base_and_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment_module, "_WINDOWS", True)
    monkeypatch.setattr(
        environment_module.os,
        "environ",
        {
            "Path": "ambient",
            "home": "home",
            "UserProfile": "profile",
            "SystemRoot": "root",
            "DOCKER_HOST": "ambient",
            "TOKEN": "secret",
        },
    )
    config = ClientConfig(inherit_environment=False, environment={"PATH": "client"})
    policy = get_execution_policy([PolicyExecutable("docker", config)])
    assert resolve_environment(
        policy.environment, {"path": "operation"}, frozen=policy.frozen_environment
    ) == {
        "path": "operation",
        "home": "home",
        "UserProfile": "profile",
        "SystemRoot": "root",
    }


def test_isolated_frozen_scope_wins_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PARENT_TOKEN", "ambient")
    config = ClientConfig(client_call=[sys.executable], inherit_environment=False)
    command = [
        *config.docker_cmd,
        "-c",
        "import os; print(os.getenv('APP_PARENT_TOKEN', 'missing'))",
    ]
    with frozen_environment_scope(config, {"APP_PARENT_TOKEN": "planned"}):
        assert run(command) == "planned"
        assert run(command) == "missing"


def test_isolated_snapshot_precedes_argument_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_PARENT_TOKEN", "ambient")
    config = ClientConfig(
        client_call=[sys.executable],
        inherit_environment=False,
        environment={"APP_CLIENT": "before"},
    )

    class MutatingArgument:
        @override
        def __str__(self) -> str:
            config.inherit_environment = True
            config.environment = {"APP_CLIENT": "after"}
            return "import os; print(os.getenv('APP_PARENT_TOKEN', 'missing') + '|' + os.environ['APP_CLIENT'])"

    assert run([*config.docker_cmd, "-c", MutatingArgument()]) == "missing|before"


@pytest.mark.parametrize("names", [None, (), ("APP_PARENT_TOKEN",)])
def test_isolated_archive_load_and_save(
    names: Collection[str] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ambient")
    monkeypatch.setenv("APP_PARENT_TOKEN", "ambient")
    driver = tmp_path / "archive.py"
    record = tmp_path / "archive.jsonl"
    driver.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[2:]\n"
        "with Path(sys.argv[1]).open('a') as f:\n"
        "    f.write(json.dumps([args[:2], os.getenv('DOCKER_HOST'), os.getenv('APP_PARENT_TOKEN')]) + '\\n')\n"
        "if args[:2] == ['image', 'inspect']:\n"
        '    print(\'[{"Id":"sha256:abc","RepoTags":["app:latest"]}]\')\n'
        "elif args[:2] == ['image', 'load']:\n"
        "    sys.stdin.buffer.read()\n"
        "    print('Loaded image: app:latest')\n"
        "else:\n"
        "    print('archive')\n"
    )
    client = DockerClient(
        client_call=[sys.executable, str(driver), str(record)],
        inherit_environment=False,
        inherited_environment_names=names,
        environment={"DOCKER_HOST": "explicit"},
    )
    assert client.image.load(b"archive") == ["app:latest"]
    saved = client.image.save("app:latest")
    assert [json.loads(line)[0] for line in record.read_text().splitlines()] == [
        ["image", "load"],
        ["image", "inspect"],
    ]
    monkeypatch.setenv("APP_PARENT_TOKEN", "at-iteration")
    assert b"".join(saved) == b"archive\n"
    assert [json.loads(line) for line in record.read_text().splitlines()] == [
        [["image", "load"], "explicit", "ambient" if bool(names) else None],
        [["image", "inspect"], "explicit", "ambient" if bool(names) else None],
        [["image", "save"], "explicit", "at-iteration" if bool(names) else None],
    ]


@pytest.mark.parametrize("save", [False, True])
def test_archive_launch_freezes_selected_values_before_argv(
    save: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ARCHIVE_SELECTED", "before")
    driver = tmp_path / "archive.py"
    record = tmp_path / "archive.jsonl"
    driver.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[2:]\n"
        "with Path(sys.argv[1]).open('a') as output:\n"
        "    output.write(json.dumps([args[:2], os.getenv('APP_ARCHIVE_SELECTED'), os.getenv('APP_CLIENT')]) + '\\n')\n"
        "if args[:2] == ['image', 'inspect']:\n"
        '    print(\'[{"Id":"sha256:abc","RepoTags":["app:latest"]}]\')\n'
        "elif args[:2] == ['image', 'load']:\n"
        "    assert sys.stdin.buffer.read() == b'archive-input'\n"
        "    print('Loaded image: app:latest')\n"
        "else:\n"
        "    print('archive-output')\n"
    )
    config = ClientConfig(
        client_call=[sys.executable, str(driver), str(record)],
        inherited_environment_names={"APP_ARCHIVE_SELECTED"},
        environment={"APP_CLIENT": "before"},
    )
    conversions = 0

    class MutatingDriver(str):
        @override
        def __str__(self) -> str:
            nonlocal conversions
            conversions += 1
            monkeypatch.setenv("APP_ARCHIVE_SELECTED", f"after-{conversions}")
            config.environment = {"APP_CLIENT": f"after-{conversions}"}
            if conversions == 2:
                config.inherited_environment_names = ()
            return super().__str__()

    config.client_call[1] = MutatingDriver(str(driver))
    client = DockerClient(client_config=config)
    if save:
        output = client.image.save("app:latest")
        assert [json.loads(line)[0] for line in record.read_text().splitlines()] == [
            ["image", "inspect"]
        ]
        assert b"".join(output) == b"archive-output\n"
        expected = [
            [["image", "inspect"], "before", "before"],
            [["image", "save"], "after-1", "after-1"],
        ]
    else:
        assert client.image.load(b"archive-input") == ["app:latest"]
        expected = [[["image", "load"], "before", "before"]]
    assert [json.loads(line) for line in record.read_text().splitlines()] == expected


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
        inherited_environment_names=(),
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
