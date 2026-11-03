from dataclasses import replace
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.admission import EndpointIdentity
from pywhales.profiles import (
    CONTROLS,
    CreateRequest,
    LogMode,
    Mount,
    PolicyError,
    PolicyReason,
    ResourceLimits,
    RuntimePolicy,
    SecurityProfile,
    create_from_plan,
    plan_create,
)
from tests.pywhales.test_profiles_fixtures import client as client
from tests.pywhales.test_profiles_fixtures import create_request as create_request
from tests.pywhales.test_profiles_fixtures import profile as profile


def test_approved_plan_translates_through_existing_create(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    plan = plan_create(profile, create_request, client)
    with patch(
        "pywhales.components.container.cli_wrapper.run", return_value="created-id"
    ) as run:
        container = create_from_plan(client, plan)
    assert container.id == "created-id"
    assert container.client_config is client.client_config
    assert run.call_args.args[0] == [
        "offline-docker",
        "create",
        "--cap-add",
        "NET_BIND_SERVICE",
        "--cap-drop",
        "ALL",
        "--cpus",
        0.5,
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1000000",
        "--log-opt",
        "max-file=2",
        "--log-opt",
        "mode=blocking",
        "--memory",
        100_000_000,
        "--memory-swap",
        100_000_000,
        "--mount",
        "type=volume,source=secret-reference,target=/run/secrets/token,readonly=true",
        "--network",
        "isolated",
        "--pids-limit",
        32,
        "--pull",
        "never",
        "--read-only",
        "--runtime",
        "runc",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        "1000:1000",
        create_request.image,
        "/app",
    ]


@pytest.mark.parametrize(
    "field",
    [
        "endpoint",
        "image.digest",
        "image.registry",
        "mounts",
        "runtime",
        "user",
        "capabilities",
        "privileged",
        "read_only",
        "security_options",
        "networks",
        "resources",
        "logging",
    ],
)
def test_policy_rejects_before_any_preflight(
    field: str,
    client: DockerClient,
    profile: SecurityProfile,
    create_request: CreateRequest,
) -> None:
    variants = {
        "endpoint": replace(create_request, endpoint=EndpointIdentity("other")),
        "image.digest": replace(create_request, image="registry.example/app:latest"),
        "image.registry": replace(
            create_request, image="evil.example/app@sha256:" + "a" * 64
        ),
        "mounts": replace(
            create_request,
            mounts=(
                Mount(
                    kind="bind",
                    source="/private-secret",
                    target="/host",
                    read_only=False,
                ),
            ),
        ),
        "runtime": replace(create_request, rootless=True),
        "user": replace(create_request, user="0"),
        "capabilities": replace(create_request, capabilities=("SYS_ADMIN",)),
        "privileged": replace(create_request, privileged=True),
        "read_only": replace(create_request, read_only=False),
        "security_options": replace(create_request, security_options=()),
        "networks": replace(create_request, networks=("host",)),
        "resources": replace(
            create_request, resources=ResourceLimits(cpus=2.0, memory_bytes=1, pids=1)
        ),
        "logging": replace(
            create_request, logging=replace(create_request.logging, max_files=9)
        ),
    }
    with (
        patch("subprocess.run") as subprocess_run,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(PolicyError) as failure,
    ):
        plan_create(profile, replace(variants[field], pull="missing"), client)
    assert failure.value.field == field
    assert "private-secret" not in str(failure.value)
    assert "private-secret" not in repr(failure.value)
    image.assert_not_called()
    subprocess_run.assert_not_called()


def test_binding_rejects_equal_but_distinct_config(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    other = DockerClient()
    with pytest.raises(PolicyError, match="endpoint"):
        plan_create(profile, create_request, other)


def test_config_change_after_planning_is_rejected_before_pull(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    plan = plan_create(profile, replace(create_request, pull="always"), client)
    client.client_config.host = "confidential-endpoint"
    with (
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(PolicyError) as error,
    ):
        create_from_plan(client, plan)
    assert error.value.field == "endpoint.binding"
    assert "confidential-endpoint" not in str(error.value)
    image.assert_not_called()


def test_rootless_requires_all_operator_qualified_controls(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    unsupported = RuntimePolicy(
        name="runc", rootless=True, supported_controls=CONTROLS - {"cpu"}
    )
    with pytest.raises(PolicyError, match="runtime"):
        plan_create(
            replace(profile, runtimes=(unsupported,)),
            replace(create_request, rootless=True),
            client,
        )


def test_direct_create_stays_permissive(client: DockerClient) -> None:
    with patch(
        "pywhales.components.container.cli_wrapper.run", return_value="direct-id"
    ) as run:
        result = client.create(
            "mutable:latest", user="0", privileged=True, pull="never"
        )
    assert result.id == "direct-id"
    assert run.call_args.args[0] == [
        "offline-docker",
        "create",
        "--privileged",
        "--pull",
        "never",
        "--user",
        "0",
        "mutable:latest",
    ]


def test_plan_summary_hides_payloads(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    result = plan_create(
        profile, replace(create_request, command=("confidential-command",)), client
    )
    assert result.summary.profile == "batch"
    assert result.summary.endpoint == "test"
    assert "confidential-command" not in repr(result)
    assert "secret-reference" not in repr(result.summary)


@pytest.mark.parametrize("cpus", [0.0, -1.0, float("nan"), float("inf"), True])
def test_invalid_cpu_is_policy_error(cpus: float) -> None:
    with pytest.raises(PolicyError):
        ResourceLimits(cpus=cpus, memory_bytes=1, pids=1)


def test_nonblocking_logging_needs_explicit_loss_approval(
    create_request: CreateRequest,
) -> None:
    with pytest.raises(PolicyError):
        replace(create_request.logging, mode=LogMode.NON_BLOCKING, buffer_bytes=1024)


@pytest.mark.parametrize(
    "source", ["/tmp/a,readonly=false", "/tmp/a\x00b", "/tmp/a\nb"]
)
def test_raw_mount_fragment_is_rejected(source: str) -> None:
    with pytest.raises(PolicyError):
        Mount(kind="bind", source=source, target="/data", read_only=True)


def test_health_interval_exact_ceiling_reaches_create(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    runtime = replace(
        profile.runtimes[0], supported_controls=CONTROLS | {"health_start_interval"}
    )
    approved = replace(profile, runtimes=(runtime,), max_health_start_interval=5)
    request = replace(create_request, health_start_interval=5)
    with (
        patch("shutil.which") as lookup,
        patch(
            "pywhales.components.container.cli_wrapper.run", return_value="created-id"
        ) as launch,
    ):
        plan = plan_create(approved, request, client)
        lookup.assert_not_called()
        create_from_plan(client, plan)
    argv = launch.call_args.args[0]
    index = argv.index("--health-start-interval")
    assert argv[index + 1] == "5s"
    assert "--health-cmd" not in argv
    assert "--health-start-period" not in argv


@pytest.mark.parametrize(
    ("ceiling", "controls", "interval", "field", "reason"),
    [
        (None, CONTROLS, 5, "health_start_interval", PolicyReason.NOT_APPROVED),
        (4, CONTROLS, 5, "health_start_interval", PolicyReason.LIMIT),
        (5, CONTROLS, 5, "runtime", PolicyReason.UNSUPPORTED),
    ],
)
def test_health_interval_rejections_are_preflight_free(
    client: DockerClient,
    profile: SecurityProfile,
    create_request: CreateRequest,
    ceiling: int | None,
    controls: frozenset[str],
    interval: int,
    field: str,
    reason: PolicyReason,
) -> None:
    runtime = replace(profile.runtimes[0], supported_controls=controls)
    approved = replace(profile, runtimes=(runtime,), max_health_start_interval=ceiling)
    request = replace(create_request, health_start_interval=interval, pull="always")
    with (
        patch("shutil.which") as lookup,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(PolicyError) as failure,
    ):
        plan_create(approved, request, client)
    assert (failure.value.field, failure.value.reason) == (field, reason)
    lookup.assert_not_called()
    image.assert_not_called()


def test_omitted_health_interval_keeps_baseline_runtime(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    assert (
        plan_create(profile, create_request, client).request.health_start_interval
        is None
    )
