"""Verify container assignments at the real child boundary, without Docker."""

import sys
from collections.abc import Collection
from pathlib import Path
from typing import Literal, assert_never
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from pywhales import DockerClient
from pywhales.components.buildx.cli_wrapper import BuildxCall
from pywhales.utils import MappedProcessStream, ProcessStream

type Operation = Literal[
    "create",
    "run",
    "execute",
    "service",
    "compose_run",
    "compose_execute",
    "compose_build",
    "build",
]
type Mode = Literal[
    "captured",
    "streamed",
    "detached",
    "tty",
    "check",
    "outline",
    "targets",
    "no_load",
    "container_load",
    "remote_load",
    "untagged_load",
]


class ChildRecord(BaseModel):
    argv: list[str]
    environment: dict[str, str]


@pytest.mark.parametrize(
    "operation,mode",
    [
        ("create", "captured"),
        ("run", "captured"),
        ("run", "streamed"),
        ("run", "detached"),
        ("run", "tty"),
        ("execute", "captured"),
        ("execute", "streamed"),
        ("execute", "detached"),
        ("execute", "tty"),
        ("service", "captured"),
        ("service", "detached"),
        ("compose_run", "captured"),
        ("compose_run", "streamed"),
        ("compose_run", "detached"),
        ("compose_run", "tty"),
        ("compose_execute", "captured"),
        ("compose_execute", "detached"),
        ("compose_execute", "tty"),
        ("compose_build", "captured"),
        ("compose_build", "streamed"),
        ("build", "captured"),
        ("build", "streamed"),
        ("build", "check"),
        ("build", "outline"),
        ("build", "targets"),
        ("build", "no_load"),
        ("build", "container_load"),
        ("build", "remote_load"),
        ("build", "untagged_load"),
    ],
)
@pytest.mark.parametrize("strict", [False, True])
def test_assignments_reach_child_without_value_arguments(
    operation: Operation, mode: Mode, tmp_path: Path, strict: bool
) -> None:
    # Given: a fake CLI that independently records each child's argv and environment.
    driver = tmp_path / "driver.py"
    records = tmp_path / "records.jsonl"
    driver.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[2:]\n"
        "with Path(sys.argv[1]).open('a') as output:\n"
        "    output.write(json.dumps({'argv': args, 'environment': dict(os.environ)}) + '\\n')\n"
        "if args[:2] == ['container', 'inspect']:\n"
        '    print(\'[{"Id":"container-id"}]\')\n'
        "elif args[:2] == ['buildx', 'inspect']:\n"
        "    print('Name: selected')\n"
        "elif args[:2] == ['buildx', 'ls']:\n"
        "    print(json.dumps({'Name': 'selected', 'Driver': os.environ['PYWHALES_TEST_DRIVER']}))\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        '    print(\'[{"Id":"sha256:built"}]\')\n'
        "else:\n"
        "    if '--iidfile' in args:\n"
        "        Path(args[args.index('--iidfile') + 1]).write_text('sha256:built')\n"
        "    print('container-id')\n"
    )
    client = DockerClient(
        strict_env_transport=strict,
        client_call=[sys.executable, str(driver), str(records)],
        environment={
            "PYWHALES_TEST_DRIVER": "docker-container"
            if mode in {"container_load", "untagged_load"}
            else "remote"
            if mode == "remote_load"
            else "docker",
            "PYWHALES_BRANCH_COLLISION": "client-value",
            "DOCKER_HOST": "client-endpoint",
        },
    )
    assignments = {
        "PYWHALES_BRANCH_SECRET": "unique-secret-transport-value",
        "PYWHALES_BRANCH_EMPTY": "",
        "PYWHALES_BRANCH_MULTILINE": "first=second\nthird",
        "PYWHALES_BRANCH_COLLISION": "container-value",
        "DOCKER_HOST": "container-endpoint",
    }
    if strict:
        del assignments["PYWHALES_BRANCH_COLLISION"]
        del assignments["DOCKER_HOST"]
    stream = mode == "streamed"
    detach = mode == "detached"
    tty = mode == "tty"
    call: BuildxCall | None = None
    match mode:
        case "check":
            call = "check"
        case "outline":
            call = "outline"
        case "targets":
            call = "targets"
        case _:
            pass

    # When: invoke each public wrapper through every supported output branch.
    match operation:
        case "create":
            result = client.container.create("app", envs=assignments, pull="never")
        case "run":
            result = client.run(
                "app",
                envs=assignments,
                pull="never",
                stream=stream,
                detach=detach,
                tty=tty,
            )
        case "execute":
            result = client.execute(
                "container-id",
                ["cmd"],
                envs=assignments,
                stream=stream,
                detach=detach,
                tty=tty,
            )
        case "service":
            result = client.service.create(
                "app", ["cmd"], envs=assignments, detach=detach
            )
        case "compose_run":
            result = client.compose.run(
                "app",
                envs=assignments,
                stream=stream,
                detach=detach,
                tty=tty,
            )
        case "compose_execute":
            result = client.compose.execute(
                "app",
                ["cmd"],
                envs=assignments,
                detach=detach,
                tty=tty,
            )
        case "compose_build":
            result = client.compose.build(build_args=assignments, stream_logs=stream)
        case "build":
            result = client.build(
                ".",
                build_args=assignments,
                stream_logs=stream,
                load=mode != "no_load",
                push=mode == "no_load",
                tags="sha256:built"
                if mode in {"container_load", "remote_load"}
                else (),
                call=call,
            )
        case unreachable:
            assert_never(unreachable)
    if isinstance(result, (ProcessStream, MappedProcessStream)):
        with result:
            list(result)

    # Then: values arrive independently of argv, including empty and multiline values.
    launches = [
        ChildRecord.model_validate_json(line)
        for line in records.read_text().splitlines()
    ]
    option = "--build-arg" if operation in {"build", "compose_build"} else "--env"
    launch = next(record for record in launches if option in record.argv)
    options = [
        launch.argv[index + 1]
        for index, word in enumerate(launch.argv)
        if word == option
    ]
    expected_options = [
        "PYWHALES_BRANCH_SECRET",
        "PYWHALES_BRANCH_EMPTY",
        "PYWHALES_BRANCH_MULTILINE",
    ]
    if not strict:
        expected_options.extend(
            [
                "PYWHALES_BRANCH_COLLISION=container-value",
                "DOCKER_HOST=container-endpoint",
            ]
        )
    assert options == expected_options
    for key in (
        "PYWHALES_BRANCH_SECRET",
        "PYWHALES_BRANCH_EMPTY",
        "PYWHALES_BRANCH_MULTILINE",
    ):
        assert launch.environment[key] == assignments[key]
        assert f"{key}={assignments[key]}" not in launch.argv
    assert assignments["PYWHALES_BRANCH_SECRET"] not in " ".join(launch.argv)
    assert launch.environment["PYWHALES_BRANCH_COLLISION"] == "client-value"
    assert launch.environment["DOCKER_HOST"] == "client-endpoint"


@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "run",
        "execute",
        "service",
        "compose_run",
        "compose_execute",
        "compose_build",
        "build",
    ],
)
@pytest.mark.parametrize(
    "key", ["DOCKER_HOST", "PYTHON_VERSION", "", "INVALID=NAME", "NULL\x00NAME"]
)
def test_strict_assignments_reject_without_disclosure(
    operation: Operation, key: str, tmp_path: Path
) -> None:
    driver = tmp_path / "reject.py"
    driver.write_text("raise AssertionError('strict rejection must precede launch')\n")
    client = DockerClient(
        client_call=[sys.executable, str(driver)], strict_env_transport=True
    )
    assignments = {key: "secret-must-not-appear"}
    with pytest.raises(ValueError) as rejected:
        match operation:
            case "create":
                client.container.create("app", envs=assignments, pull="never")
            case "run":
                client.run("app", envs=assignments, pull="never")
            case "execute":
                client.execute("app", ["cmd"], envs=assignments)
            case "service":
                client.service.create("app", ["cmd"], envs=assignments)
            case "compose_run":
                client.compose.run("app", envs=assignments)
            case "compose_execute":
                client.compose.execute("app", ["cmd"], envs=assignments)
            case "compose_build":
                client.compose.build(build_args=assignments)
            case "build":
                client.build(".", build_args=assignments)
            case unreachable:
                assert_never(unreachable)
    assert "secret-must-not-appear" not in str(rejected.value)
    assert rejected.value.__cause__ is None


@pytest.mark.parametrize("operation", ["build", "compose_build"])
def test_strict_build_rejection_precedes_lookup(
    operation: Literal["build", "compose_build"],
) -> None:
    client = DockerClient(client_call=["missing-executable"], strict_env_transport=True)
    with pytest.raises(ValueError):
        if operation == "build":
            client.build(".", build_args={"PYTHON_VERSION": "secret"})
        else:
            client.compose.build(build_args={"PYTHON_VERSION": "secret"})


@pytest.mark.parametrize("operation", ["execute", "compose_build"])
@pytest.mark.parametrize("names", [None, (), ("APP_APPROVED",)])
def test_isolated_forwarding_reaches_real_child(
    operation: str,
    names: Collection[str] | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_COLLISION", "ambient")
    monkeypatch.setenv("DOCKER_HOST", "ambient-endpoint")
    monkeypatch.setenv("APP_APPROVED", "selected")
    driver = tmp_path / "driver.py"
    record = tmp_path / "record.json"
    driver.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({'argv': sys.argv[2:], 'environment': dict(os.environ)}))\n"
    )
    client = DockerClient(
        client_call=[sys.executable, str(driver), str(record)],
        inherit_environment=False,
        inherited_environment_names=names,
        strict_env_transport=True,
    )
    if operation == "execute":
        client.execute("container", ["cmd"], envs={"APP_COLLISION": "forwarded"})
    else:
        client.compose.build(build_args={"APP_COLLISION": "forwarded"})
    launch = ChildRecord.model_validate_json(record.read_text())
    assert launch.environment["APP_COLLISION"] == "forwarded"
    assert "DOCKER_HOST" not in launch.environment
    assert "APP_COLLISION" in launch.argv
    assert "APP_COLLISION=forwarded" not in launch.argv
    assert launch.environment.get("APP_APPROVED") == (
        "selected" if bool(names) else None
    )
    if names is not None:
        assert "HOME" not in launch.environment
        assert "PATH" not in launch.environment


@pytest.mark.parametrize("operation", ["compose_build", "build"])
@pytest.mark.parametrize("selected", [False, True], ids=["overlay", "ambient"])
def test_selected_collision_rejects_before_lookup_or_image_preflight(
    operation: str, selected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_COLLISION", "ambient")
    client = DockerClient(
        inherited_environment_names={"APP_COLLISION"} if selected else (),
        environment=None if selected else {"APP_COLLISION": "client"},
        strict_env_transport=True,
    )
    with (
        patch("shutil.which") as lookup,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(ValueError) as caught,
    ):
        if operation == "compose_build":
            client.compose.build(build_args={"APP_COLLISION": "private"})
        else:
            client.build(".", build_args={"APP_COLLISION": "private"})
    assert "private" not in str(caught.value)
    lookup.assert_not_called()
    image.assert_not_called()
