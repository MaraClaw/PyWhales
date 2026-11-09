from dataclasses import replace
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.profiles import (
    CreateRequest,
    EndpointBinding,
    Mount,
    PolicyError,
    PolicyReason,
    SecurityProfile,
    create_from_plan,
    plan_create,
)
from pywhales.telemetry import TelemetryRecord
from tests.pywhales.test_profiles_fixtures import client as client
from tests.pywhales.test_profiles_fixtures import create_request as create_request
from tests.pywhales.test_profiles_fixtures import profile as profile


def test_cached_endpoint_override_rejects_before_pull(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    plan = plan_create(profile, replace(create_request, pull="always"), client)
    client.client_config._client_call_with_path = [
        "offline-docker",
        "--host",
        "tcp://other:2375",
    ]
    with (
        patch("subprocess.run") as launch,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(PolicyError, match=r"endpoint\.binding"),
    ):
        create_from_plan(client, plan)
    launch.assert_not_called()
    image.assert_not_called()


def test_changed_inherited_names_reject_before_preparation_and_pull(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    plan = plan_create(profile, replace(create_request, pull="always"), client)
    client.client_config.inherited_environment_names = frozenset({"APP_APPROVED"})
    with (
        patch.object(EndpointBinding, "_prepare") as prepare,
        patch("shutil.which") as lookup,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        patch("subprocess.run") as launch,
        pytest.raises(PolicyError) as caught,
    ):
        create_from_plan(client, plan)
    assert caught.value.field == "endpoint.binding"
    assert caught.value.reason == PolicyReason.CONFLICT
    prepare.assert_not_called()
    lookup.assert_not_called()
    image.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("field", ["resources", "logging", "endpoint", "mounts"])
def test_missing_required_component_is_typed_error(
    field: str, create_request: CreateRequest
) -> None:
    with pytest.raises(PolicyError):
        replace(create_request, **{field: None})


@pytest.mark.parametrize("field", ["source", "target"])
@pytest.mark.parametrize("value", [None, 42, ["/payload-private"]])
def test_malformed_mount_path_is_typed_error(field: str, value: object) -> None:
    mount = Mount(kind="bind", source="/safe", target="/data", read_only=True)
    with pytest.raises(PolicyError) as caught:
        replace(mount, **{field: value})
    assert caught.value.field == f"mounts.{field}"
    assert "payload-private" not in str(caught.value)


def test_execution_owned_resolution_is_reusable_then_prefix_is_fixed(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    client.client_config._client_call_with_path = None
    binding = EndpointBinding(create_request.endpoint, client.client_config)
    profile = replace(profile, bindings=(binding,))
    with patch("shutil.which") as lookup:
        plan = plan_create(profile, create_request, client)
    lookup.assert_not_called()
    with (
        patch("shutil.which", return_value="/approved/docker") as lookup,
        patch(
            "pywhales.components.container.cli_wrapper.run", return_value="created-id"
        ) as launch,
    ):
        create_from_plan(client, plan)
        create_from_plan(client, plan)
    lookup.assert_called_once_with("docker")
    assert launch.call_count == 2
    client.client_config._client_call_with_path = ["/different/docker"]
    with pytest.raises(PolicyError):
        plan_create(profile, create_request, client)


def test_approved_observer_is_retained_by_reference(
    client: DockerClient, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    records: list[TelemetryRecord] = []

    class Observer:
        def __call__(self, record: TelemetryRecord) -> None:
            records.append(record)

    client.client_config.telemetry_observer = Observer()
    binding = EndpointBinding(create_request.endpoint, client.client_config)
    result = plan_create(replace(profile, bindings=(binding,)), create_request, client)
    assert result.request == create_request
    assert records == []


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "private-value"])
@pytest.mark.parametrize("target", ["request", "profile"])
def test_health_interval_inputs_are_exact_positive_integers(
    value: object, target: str, profile: SecurityProfile, create_request: CreateRequest
) -> None:
    field = (
        "health_start_interval" if target == "request" else "max_health_start_interval"
    )
    with pytest.raises(PolicyError) as caught:
        replace(create_request if target == "request" else profile, **{field: value})
    assert caught.value.field == (field if target == "request" else f"profile.{field}")
    assert caught.value.reason == PolicyReason.INVALID
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("tamper", ["request", "ceiling"])
def test_tampered_health_plan_is_rejected_before_preparation(
    tamper: str,
    client: DockerClient,
    profile: SecurityProfile,
    create_request: CreateRequest,
) -> None:
    runtime = replace(
        profile.runtimes[0],
        supported_controls=profile.runtimes[0].supported_controls
        | {"health_start_interval"},
    )
    approved = replace(profile, runtimes=(runtime,), max_health_start_interval=5)
    request = replace(create_request, health_start_interval=5, pull="always")
    plan = plan_create(approved, request, client)
    if tamper == "request":
        object.__setattr__(request, "health_start_interval", 6)
    else:
        object.__setattr__(approved, "max_health_start_interval", True)
    with (
        patch("shutil.which") as lookup,
        patch("pywhales.components.image.cli_wrapper.ImageCLI") as image,
        pytest.raises(PolicyError) as caught,
    ):
        create_from_plan(client, plan)
    assert caught.value.reason == (
        PolicyReason.LIMIT if tamper == "request" else PolicyReason.INVALID
    )
    lookup.assert_not_called()
    image.assert_not_called()
