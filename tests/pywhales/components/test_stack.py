import time
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from pywhales import DockerClient, Service
from pywhales.client_config import ClientConfig
from pywhales.components.stack.cli_wrapper import Stack
from pywhales.exceptions import NotASwarmManager
from pywhales.utils import PROJECT_ROOT
from tests.pywhales.components.hydration_fakes import (
    INSPECT_FAILURES,
    FakeCli,
    fake_daemon,
    identity_of,
)


@pytest.fixture
def stack(
    docker_client: DockerClient, swarm_mode: None, tmp_path: Path
) -> Generator[Stack]:
    """Deploy the fixed some_stack project inside the fixture-owned Swarm.

    Requires a disposable daemon and its Swarm setup. Use small multi-platform
    services and let the scale CLI wait for convergence before listing tasks.
    Teardown waits for the stack's task containers and networks to disappear,
    because `stack remove` returns first and a force-leave with attached task
    containers leaves a stale `some_stack_default` network that the next
    deploy reuses instead of creating.

    Yields:
        The converged stack handle, removed during teardown even on failure.
    """
    compose_file = tmp_path / "stack.yml"
    compose_file.write_text(
        "services:\n"
        + "".join(
            f"  {name}:\n    image: busybox:1.37.0\n    command: [sleep, '3600']\n"
            "    init: true\n"
            for name in ("app", "db", "influxdb", "agent")
        )
    )
    docker_client.image.pull("busybox:1.37.0")
    some_stack = docker_client.stack.deploy(
        "some_stack", [compose_file], resolve_image="never"
    )
    try:
        docker_client.service.scale(
            dict.fromkeys(some_stack.services(), 1), detach=False, timeout=60
        )
        yield some_stack
    finally:
        some_stack.remove()
        namespace = ("label", "com.docker.stack.namespace=some_stack")
        deadline = time.monotonic() + 60
        while docker_client.container.list(
            all=True, filters=[namespace]
        ) or docker_client.network.list(filters=[namespace]):
            assert time.monotonic() < deadline, "some_stack resources remain"
            time.sleep(0.25)


@pytest.mark.usefixtures("stack")
def test_services_inspect(docker_client: DockerClient) -> None:
    all_services = docker_client.service.list()
    assert len(all_services) == 4
    assert set(all_services) == set(docker_client.stack.services("some_stack"))
    assert "name='some_stack'" in repr(docker_client.stack.list())


@pytest.mark.parametrize(
    "make_empty",
    [
        pytest.param(list, id="list"),
        pytest.param(tuple, id="tuple"),
        pytest.param(lambda: (item for item in ()), id="generator"),
    ],
)
def test_remove_empty_stack_list(make_empty: Callable[[], Iterable[str]]) -> None:
    client = DockerClient(client_call=["not-installed"])
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch("subprocess.run", side_effect=AssertionError("subprocess")),
    ):
        client.stack.remove(make_empty())


def test_stack_ps_and_services(docker_client: DockerClient, stack: Stack) -> None:
    all_services = docker_client.service.list()

    assert set(all_services) == set(stack.services())

    stack_tasks = set(docker_client.stack.ps("some_stack"))
    assert stack_tasks == set(stack.ps())

    services_to_inspect: list[str | Service] = [*all_services]
    services_tasks = set(docker_client.service.ps(services_to_inspect))
    assert stack_tasks == services_tasks
    assert len(stack_tasks) > 0
    for task in stack_tasks:
        assert task.desired_state == "running"
        assert task.status is not None
        assert task.status.state == "running"


@pytest.mark.usefixtures("swarm_mode")
def test_stack_variables(docker_client: DockerClient) -> None:
    docker_client.stack.deploy(
        "other_stack",
        [PROJECT_ROOT / "tests/pywhales/components/test-stack-file.yml"],
        variables={"SOME_VARIABLE": "hello-world"},
    )

    agent_service = docker_client.service.inspect("other_stack_agent")
    expected = "SOME_OTHER_VARIABLE=hello-world"
    assert agent_service.spec is not None
    assert agent_service.spec.task_template is not None
    assert agent_service.spec.task_template.container_spec is not None
    assert agent_service.spec.task_template.container_spec.env is not None
    assert expected in agent_service.spec.task_template.container_spec.env

    docker_client.stack.remove("other_stack")
    time.sleep(1)


@pytest.mark.usefixtures("swarm_mode")
def test_stack_env_files(docker_client: DockerClient, tmp_path: Path) -> None:
    env_file = tmp_path / "some.env"
    env_file.write_text('SOME_VARIABLE="--tls=true" # some var \n # some comment')
    third_stack = docker_client.stack.deploy(
        "third_stack",
        [PROJECT_ROOT / "tests/pywhales/components/test-stack-file.yml"],
        env_files=[env_file],
    )

    agent_service = docker_client.service.inspect("third_stack_agent")
    expected = 'SOME_OTHER_VARIABLE="--tls=true"'
    assert agent_service.spec is not None
    assert agent_service.spec.task_template is not None
    assert agent_service.spec.task_template.container_spec is not None
    assert agent_service.spec.task_template.container_spec.env is not None
    assert expected in agent_service.spec.task_template.container_spec.env

    assert docker_client.stack.list() == [third_stack]
    time.sleep(1)
    docker_client.stack.remove(third_stack)
    time.sleep(1)


@pytest.mark.parametrize("raw_diagnostics", [False, True])
@pytest.mark.parametrize("name", ["ld_preload", "DYLD_LIBRARY_PATH"])
@pytest.mark.parametrize("from_file", [False, True], ids=["mapping", "file"])
def test_stack_rejects_loader_overlay_before_lookup(
    tmp_path: Path, raw_diagnostics: bool, name: str, from_file: bool
) -> None:
    value = "stack-interpolation-secret"
    env_file = tmp_path / "stack.env"
    if from_file:
        env_file.write_text(f"# deployment variables\n{name}={value}\n")
    client = DockerClient(raw_diagnostics=raw_diagnostics)
    with (
        patch("shutil.which", side_effect=AssertionError("executable lookup")),
        patch.object(
            ClientConfig,
            "get_client_call_with_path",
            side_effect=AssertionError("command lookup"),
        ),
        patch(
            "pywhales.components.stack.cli_wrapper.run",
            side_effect=AssertionError("launch"),
        ),
        pytest.raises(ValueError) as rejected,
    ):
        client.stack.deploy(
            "web",
            env_files=[env_file] if from_file else [],
            variables={} if from_file else {name: value},
        )

    for private in (name, name.upper(), value):
        assert private not in str(rejected.value)
        assert private not in repr(rejected.value.args)
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


def test_deploy_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.stack.deploy(
            "some_stack",
            [PROJECT_ROOT / "tests/pywhales/components/test-stack-file.yml"],
        )

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_ps_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.stack.ps("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_list_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.stack.list()

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_remove_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.stack.remove("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


def test_services_not_swarm_manager(docker_client: DockerClient) -> None:
    with pytest.raises(NotASwarmManager) as e:
        docker_client.stack.services("dodo")

    assert e.value.stderr is not None
    assert "not a swarm manager" in e.value.stderr.lower()


SINGLE_SELECTIONS = [
    pytest.param(lambda: "alpha", id="str"),
    pytest.param(lambda: ["alpha"], id="list"),
    pytest.param(lambda: (item for item in ["alpha"]), id="generator"),
]


@pytest.mark.parametrize("make_selection", SINGLE_SELECTIONS)
def test_stack_remove_single_selection_builds_argv_once(
    make_selection: Callable[[], str | Iterable[str]],
) -> None:
    client = DockerClient()
    client.client_config._client_call_with_path = ["docker-test"]
    with patch(
        "subprocess.run", return_value=CompletedProcess([], 0, b"", b"")
    ) as command:
        client.stack.remove(make_selection())
    command.assert_called_once()
    assert command.call_args.args[0] == ["docker-test", "stack", "remove", "alpha"]


def test_default_stack_services_resolves_ids_in_batches_and_stays_lazy() -> None:
    ids = [identity_of("service", index) for index in range(101)]
    cli = FakeCli("service", ids)
    with fake_daemon(cli) as client:
        services = client.stack.services("alpha")
        assert [service.id for service in services] == ids
        assert cli.inspects() == []
        spec = services[0].spec
        assert spec is not None and spec.name == "ser-0000"

    truncated = [identity[:12] for identity in ids]
    assert cli.lists() == [["docker-test", "stack", "services", "--quiet", "alpha"]]
    assert cli.resolves() == [truncated[:100], truncated[100:]]
    assert cli.inspects() == [[ids[0]]]
    assert len(cli.launches) == 4


@pytest.mark.parametrize("stage", ["resolution", "inspection"])
def test_stack_services_skips_vanished_services_in_the_affected_chunk(
    stage: str,
) -> None:
    ids = [identity_of("service", index) for index in range(6)]
    cli = FakeCli("service", ids)
    cli.reorder = True
    if stage == "resolution":
        cli.vanish_on_list = {ids[4]}
    else:
        cli.vanish_on_resolve = {ids[4]}

    with fake_daemon(cli, chunk_size=3) as client:
        stack = Stack(client.client_config, "alpha")
        services = stack.services(filters={"mode": "global"}, hydrate=True)
        assert [service.id for service in services] == [*ids[:4], ids[5]]
        assert [
            service.spec.name for service in services if service.spec is not None
        ] == ["ser-0000", "ser-0001", "ser-0002", "ser-0003", "ser-0005"]

    truncated = [identity[:12] for identity in ids]
    if stage == "resolution":
        assert cli.resolves() == [
            truncated[:3],
            truncated[3:],
            truncated[3:4],
            truncated[4:],
            truncated[4:5],
            truncated[5:],
        ]
        assert cli.inspects() == [ids[:3], [ids[3], ids[5]]]
    else:
        assert cli.resolves() == [truncated[:3], truncated[3:]]
        assert cli.inspects() == [
            ids[:3],
            ids[3:],
            ids[3:4],
            ids[4:],
            ids[4:5],
            ids[5:],
        ]
    assert cli.lists() == [
        [
            "docker-test",
            "stack",
            "services",
            "--quiet",
            "--filter",
            "mode=global",
            "alpha",
        ]
    ]
    assert len(cli.launches) == 9


@pytest.mark.parametrize("formatted", [True, False], ids=["resolution", "inspection"])
@pytest.mark.parametrize("failure", list(INSPECT_FAILURES))
def test_stack_services_propagates_other_failures(
    failure: str, formatted: bool
) -> None:
    ids = [identity_of("service", index) for index in range(3)]
    cli = FakeCli("service", ids)
    expected = INSPECT_FAILURES[failure]
    with (
        fake_daemon(cli) as client,
        patch(
            "subprocess.run",
            side_effect=cli.failing({"inspect"}, failure, formatted=formatted),
        ),
        pytest.raises(expected) as caught,
    ):
        client.stack.services("alpha", hydrate=not formatted)

    assert type(caught.value) is expected
    assert cli.resolves() == [[identity[:12] for identity in ids]]
    assert cli.inspects() == ([] if formatted else [ids])
