import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest

from pywhales import DockerClient, observation
from pywhales._environment import (
    consume_frozen_environment,
    container_environment,
    frozen_environment_scope,
    resolve_environment,
)
from pywhales.client_config import ClientConfig
from pywhales.exceptions import DockerException, ObservationLimitError
from pywhales.inventory import InspectionPolicy
from pywhales.utils import ProcessStream

type Operation = Literal[
    "create", "run", "execute", "service", "compose_run", "compose_execute"
]


@pytest.fixture
def env_client(tmp_path: Path) -> DockerClient:
    driver = tmp_path / "env_cli.py"
    driver.write_text(
        """import json, os, sys
from pathlib import Path
args = sys.argv[2:]
with Path(sys.argv[1]).open('a') as output:
    output.write(json.dumps({'argv': args, 'environment': dict(os.environ)}) + '\\n')
if os.environ.get('PYWHALES_TEST_FAIL'):
    sys.exit(3)
if args[:2] == ['container', 'inspect']:
    print('[{"Id":"container-id"}]')
elif args[:2] == ['image', 'inspect']:
    print('[{"Id":"sha256:image"}]')
else:
    print('container-id')
"""
    )
    client = DockerClient()
    client.client_config._client_call_with_path = [
        sys.executable,
        str(driver),
        str(tmp_path / "launches.jsonl"),
    ]
    return client


def invoke(client: DockerClient, operation: Operation, branch: str) -> object:
    envs = {
        "PYWHALES_PRIVATE_APP_TOKEN": "unique=value\nwith spaces",
        "APP_EMPTY": "",
        "APP_NUMBER": "42",
    }
    stream, detach, tty = branch == "stream", branch == "detach", branch == "tty"
    match operation:
        case "create":
            return client.container.create(
                "image", envs=envs, env_files=["first", "second"]
            )
        case "run":
            return client.run(
                "image",
                envs=envs,
                env_files=["first", "second"],
                stream=stream,
                detach=detach,
                tty=tty,
            )
        case "execute":
            return client.execute(
                "container",
                ["command"],
                envs=envs,
                env_files=["first", "second"],
                stream=stream,
                detach=detach,
                tty=tty,
            )
        case "service":
            return client.service.create(
                "image", None, envs=envs, env_files=["first", "second"]
            )
        case "compose_run":
            return client.compose.run(
                "app", envs=envs, stream=stream, detach=detach, tty=tty
            )
        case "compose_execute":
            return client.compose.execute(
                "app", ["command"], envs=envs, detach=detach, tty=tty
            )


@pytest.mark.parametrize(
    "operation,branch",
    [
        ("create", "captured"),
        ("service", "captured"),
        *[
            (op, branch)
            for op in ("run", "execute", "compose_run")
            for branch in ("captured", "stream", "detach", "tty")
        ],
        *[("compose_execute", branch) for branch in ("captured", "detach", "tty")],
    ],
)
def test_values_reach_child_without_argv(
    env_client: DockerClient, tmp_path: Path, operation: Operation, branch: str
) -> None:
    result = invoke(env_client, operation, branch)
    if isinstance(result, ProcessStream):
        with result:
            list(result)
    launches = [
        json.loads(line)
        for line in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    launch = next(item for item in launches if "--env" in item["argv"])
    assert (
        launch["environment"]["PYWHALES_PRIVATE_APP_TOKEN"]
        == "unique=value\nwith spaces"
    )
    assert launch["environment"]["APP_EMPTY"] == ""
    assert launch["environment"]["APP_NUMBER"] == "42"
    args = launch["argv"]
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "--env"] == [
        "PYWHALES_PRIVATE_APP_TOKEN",
        "APP_EMPTY",
        "APP_NUMBER",
    ]
    assert "unique=value" not in " ".join(args)
    assert "PYWHALES_PRIVATE_APP_TOKEN" not in os.environ
    if not operation.startswith("compose"):
        assert [args[i + 1] for i, arg in enumerate(args) if arg == "--env-file"] == [
            "first",
            "second",
        ]


@pytest.mark.parametrize(
    "name",
    [
        "DOCKER_HOST",
        "COMPOSE_FILE",
        "BUILDX_BUILDER",
        "BUILDKIT_HOST",
        "http_proxy",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "GODEBUG",
    ],
)
def test_reserved_assignments_stay_literal(name: str) -> None:
    assert container_environment({name: "container-value"}, ClientConfig()) == (
        [f"{name}=container-value"],
        {},
    )


@pytest.mark.parametrize("name", ["", "bad=name", "bad\0name"])
def test_invalid_environment_names_stay_literal(name: str) -> None:
    assert container_environment({name: False}, ClientConfig()) == (
        [f"{name}=False"],
        {},
    )


def test_frozen_collision_check_preserves_one_use_snapshot() -> None:
    config = ClientConfig(environment={"CLIENT_ONLY": "client"})
    with frozen_environment_scope(config, {"FROZEN_ONLY": "frozen"}):
        assert container_environment(
            {"FROZEN_ONLY": "container", "CLIENT_ONLY": "app"}, config
        ) == (["FROZEN_ONLY=container", "CLIENT_ONLY"], {"CLIENT_ONLY": "app"})
        assert consume_frozen_environment(config) == {"FROZEN_ONLY": "frozen"}


def test_client_and_ambient_collisions_stay_literal() -> None:
    ambient = next(iter(os.environ))
    assert container_environment(
        {ambient: "container", "CLIENT_ONLY": "container"},
        ClientConfig(environment={"CLIENT_ONLY": "client"}),
    ) == ([f"{ambient}=container", "CLIENT_ONLY=container"], {})


def test_windows_client_collision_stays_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pywhales._environment._WINDOWS", True)
    assert container_environment(
        {"app_key": "container"}, ClientConfig(environment={"APP_KEY": "client"})
    ) == (["app_key=container"], {})


def test_values_and_nonstring_keys_keep_stringification() -> None:
    assert container_environment(
        {"APP_NUMBER": 42, "APP_BOOL": False, "APP_NUL": "x\0y", 7: "value"},
        ClientConfig(),
    ) == (
        ["APP_NUMBER", "APP_BOOL", "APP_NUL=x\0y", "7=value"],
        {"APP_NUMBER": "42", "APP_BOOL": "False"},
    )


@pytest.mark.parametrize("raw", [False, True])
def test_exception_command_never_contains_moved_values(
    env_client: DockerClient, raw: bool
) -> None:
    env_client.client_config.environment = {"PYWHALES_TEST_FAIL": "1"}
    env_client.client_config.raw_diagnostics = raw
    with pytest.raises(DockerException) as raised:
        env_client.execute(
            "container", ["command"], envs={"APP_SECRET": "hidden-value"}
        )
    assert "hidden-value" not in " ".join(raised.value.docker_command)
    assert "hidden-value" not in str(raised.value)
    assert "APP_SECRET" in raised.value.docker_command


def test_windows_mapping_collisions_all_stay_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pywhales._environment._WINDOWS", True)
    assert container_environment(
        {"app_key": "one", "APP_KEY": "two"}, ClientConfig()
    ) == (["app_key=one", "APP_KEY=two"], {})


def test_collision_snapshot_is_the_launch_environment() -> None:
    config = ClientConfig(environment={"CLIENT_SNAPSHOT": "before"})
    _, overrides = container_environment({"APP_VALUE": "container"}, config)
    config.environment = {"CLIENT_SNAPSHOT": "after", "APP_VALUE": "new-collision"}
    effective = resolve_environment(config.environment, overrides)
    assert effective["CLIENT_SNAPSHOT"] == "before"
    assert effective["APP_VALUE"] == "container"


@pytest.mark.parametrize(
    "assignments",
    [
        {"APP_NUL": "private\0value"},
        {7: "private"},
        {"PYTHON_VERSION": "private"},
        {"APP_COLLISION": "private"},
    ],
)
def test_strict_transport_rejects_every_literal_fallback(
    assignments: dict[str | int, str],
) -> None:
    config = ClientConfig(
        strict_env_transport=True, environment={"APP_COLLISION": "client"}
    )
    with pytest.raises(ValueError) as error:
        container_environment(assignments, config)
    assert "private" not in str(error.value)


def test_strict_windows_equivalent_entries_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pywhales._environment._WINDOWS", True)
    with pytest.raises(ValueError):
        container_environment(
            {"app_key": "one", "APP_KEY": "two"},
            ClientConfig(strict_env_transport=True),
        )


def test_strict_frozen_snapshot_controls_collision_and_launch() -> None:
    config = ClientConfig(strict_env_transport=True, environment={"APP_VALUE": "newer"})
    with frozen_environment_scope(config, {"FROZEN_VALUE": "frozen"}):
        arguments, overrides = container_environment({"APP_VALUE": "selected"}, config)
        assert arguments == ["APP_VALUE"]
        assert resolve_environment(config.environment, overrides) == {
            "FROZEN_VALUE": "frozen",
            "APP_VALUE": "selected",
        }
        assert consume_frozen_environment(config) == {"FROZEN_VALUE": "frozen"}


def test_moved_value_is_charged_to_launch_environment(
    env_client: DockerClient, tmp_path: Path
) -> None:
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )
    with observation(policy), pytest.raises(ObservationLimitError) as raised:
        env_client.execute(
            "container", ["command"], envs={"APP_HUGE": "x" * 10_000_000}
        )
    assert raised.value.reason.startswith("native")
    assert not (tmp_path / "launches.jsonl").exists()
