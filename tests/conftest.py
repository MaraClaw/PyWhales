"""Shared pytest runtime selection and destructive Docker integration fixtures.

Use a disposable daemon colocated with the runner: registry fixtures bind port
5000 and change local login state, while Swarm fixtures change daemon membership.
The docker marker follows fixture dependencies, not every possible Docker call.
"""

import logging
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import assert_never

import pytest

from pywhales import DockerClient
from pywhales._reclaim import _ensure_worker

logger = logging.getLogger(__name__)

#
# -----------------------------------------------------------------------------
# Fixtures


@pytest.fixture(scope="session", autouse=True)
def stream_reclaimer() -> None:
    """Start the process-lifetime stream reclaimer before any test runs.

    Streams start it lazily on first construction. Tests that compare
    ``threading.enumerate()`` around a launch would otherwise count that
    one-time start as a leaked worker thread.
    """
    _ensure_worker()


@pytest.fixture(scope="session")
def docker_client(pytestconfig: pytest.Config) -> DockerClient:
    """Return the session client after a ten-second Docker version probe.

    A missing executable, unsuccessful probe, or timeout skips runtime-dependent
    tests unless --no-runtime-skip requests failure. This does not gate separately
    constructed clients, global clients, or every plugin requirement.

    Args:
        pytestconfig: Supplies --docker-exe and --no-runtime-skip selections.

    Returns:
        The shared client using the selected executable and daemon environment.
    """
    ctr_exe = pytestconfig.getoption("--docker-exe")
    assert isinstance(ctr_exe, str)
    client = DockerClient(client_call=[ctr_exe])
    try:
        # TODO: Implement 'DockerClient.version' and use that instead.
        subprocess.run(
            [ctr_exe, "version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        match exc:
            case subprocess.CalledProcessError():
                reason = "\n" + exc.stderr.strip()
            case subprocess.TimeoutExpired():
                reason = f"timed out after {exc.timeout} seconds"
            case FileNotFoundError():
                reason = "executable not found"
            case unreachable:
                assert_never(unreachable)
        logger.warning(
            "Unable to get Docker version with command '%s version': %s",
            ctr_exe,
            reason,
        )
        if pytestconfig.getoption("--no-runtime-skip"):
            pytest.fail("docker unavailable")
        else:
            pytest.skip("docker unavailable")

    return client


@pytest.fixture(scope="function")
def ctr_client(docker_client: DockerClient) -> DockerClient:
    """Return the session client without isolating or resetting daemon state."""
    return docker_client


@pytest.fixture(scope="function")
def docker_registry(docker_client: DockerClient) -> Generator[str]:
    """Yield a port-5000 registry after login, then log out and remove it.

    Requires a colocated disposable Docker daemon, available host port 5000,
    and access to the helper and registry images. Changes local login state.

    Yields:
        The authenticated test registry address, localhost:5000.
    """
    yield from _docker_registry(docker_client)


@pytest.fixture(scope="function")
def docker_registry_without_login(
    docker_client: DockerClient,
) -> Generator[str]:
    """Yield a temporary authenticated registry without changing login state.

    Uses the same port-5000 container setup as docker_registry but performs
    neither login nor logout. Requires a colocated disposable Docker daemon.

    Yields:
        The test registry address, localhost:5000, without client credentials.
    """
    yield from _docker_registry(docker_client, login=False)


def _docker_registry(docker_client: DockerClient, login: bool = True) -> Generator[str]:
    """Start a local test registry and release its container during teardown.

    Setup can pull helper and registry images and binds daemon host port 5000.
    Temporary authentication files are removed when their context exits. Optional
    logout runs after the yield even if the consuming test raises.

    Args:
        docker_client: Client targeting a disposable daemon colocated with pytest.
        login: Log in before yielding and log out afterward when true.

    Yields:
        The registry address localhost:5000 after its startup delay.
    """
    encrypted_password = docker_client.run(
        "mhenry07/apache2-utils",
        ["htpasswd", "-Bbn", "my_user", "my_password"],
        remove=True,
    )
    with tempfile.TemporaryDirectory() as tmp_path:
        tmp_path = Path(tmp_path)
        htpasswd_file = tmp_path / "htpasswd"
        htpasswd_file.write_text(encrypted_password)
        registry = docker_client.container.create(
            "registry:2",
            remove=True,
            envs={
                "REGISTRY_AUTH": "htpasswd",
                "REGISTRY_AUTH_HTPASSWD_REALM": "Registry Realm",
                "REGISTRY_AUTH_HTPASSWD_PATH": "/tmp/htpasswd",
            },
            publish=[(5000, 5000)],
        )
        with registry:
            registry.copy_to(htpasswd_file, "/tmp/htpasswd")
            registry.start()
            time.sleep(1.5)
            try:
                if login:
                    docker_client.login(
                        "localhost:5000", username="my_user", password="my_password"
                    )
                yield "localhost:5000"
            finally:
                if login:
                    docker_client.logout("localhost:5000")


@pytest.fixture(scope="function")
def swarm_mode(docker_client: DockerClient) -> Generator[None]:
    """Initialize Swarm and force-leave it during normal fixture teardown.

    Uses the fixed advertise address 172.31.155.129 and changes daemon membership.
    Requires an eligible disposable daemon rather than an existing valued Swarm.

    Yields:
        Control while tests operate on the initialized Swarm.
    """
    docker_client.swarm.init(advertise_address="172.31.155.129")
    yield
    docker_client.swarm.leave(force=True)
    time.sleep(1)


#
# -----------------------------------------------------------------------------
# Pytest hooks


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register executable selection and strict runtime-availability options."""

    pywhales_group = parser.getgroup("pywhales")
    pywhales_group.addoption(
        "--docker-exe",
        metavar="EXE",
        default="docker",
        help="Path to the docker executable to use in the unit tests."
        "Defaults to docker.",
    )
    pywhales_group.addoption(
        "--no-runtime-skip",
        action="store_true",
        help="Do not skip tests corresponding to a container runtime that is not available",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Treat Pydantic 2 deprecation warnings as test failures."""
    # Filter warnings
    config.addinivalue_line(
        "filterwarnings", "error::pydantic.PydanticDeprecatedSince20"
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark tests whose fixture dependency closure includes docker_client.

    This does not identify global-client calls or prevent import-time probes.
    """
    # Apply marks to testcases.
    for item in items:
        assert isinstance(item, pytest.Function)
        if "docker_client" in item.fixturenames:
            item.add_marker("docker")
