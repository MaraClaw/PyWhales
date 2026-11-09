from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import ModuleType
from typing import assert_type, get_args
from unittest.mock import patch

import pytest

from pywhales._inventory_dispatch import make_resource, resource_kind
from pywhales.client_config import ClientConfig
from pywhales.components.buildx.cli_wrapper import Builder
from pywhales.components.buildx.models import BuilderInspectResult
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.components.manifest.models import ManifestListInspectResult
from pywhales.components.network.models import NetworkInspectResult
from pywhales.inventory import (
    InspectionPolicy,
    InventoryKind,
    InventoryPayload,
    InventoryRecord,
    InventoryRequest,
    InventoryResource,
    InventorySnapshot,
    capture_inventory,
)


def _check_inventory_types(
    request: InventoryRequest, resource: InventoryResource, policy: InspectionPolicy
) -> None:
    assert_type(request.kind, InventoryKind)
    assert_type(request.references, tuple[str, ...])
    assert_type(policy.max_total_stream_bytes, int | None)
    assert_type(policy.max_total_stdin_bytes, int | None)
    assert_type(policy.max_total_io_bytes, int | None)
    assert_type(policy.max_total_archive_stderr_bytes, int | None)
    assert_type(policy.max_total_launch_attempts, int | None)
    assert_type(
        capture_inventory([request, resource], policy=policy), InventorySnapshot
    )
    manifest = ManifestList(request.client_config, "example", is_immutable_id=True)
    assert_type(resource_kind(manifest), InventoryKind)
    assert_type(manifest._inspect_result, ManifestListInspectResult | None)
    assert_type(capture_inventory([manifest], policy=policy), InventorySnapshot)


def test_manifest_list_is_an_inventory_kind_resource_and_payload() -> None:
    assert "manifest_list" in get_args(InventoryKind.__value__)
    assert ManifestList in get_args(InventoryResource.__value__)
    assert ManifestListInspectResult in get_args(InventoryPayload.__value__)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_policy_rejects_invalid_limits(value: int) -> None:
    with pytest.raises(ValueError):
        InspectionPolicy(max_references=value, max_argv_bytes=1, max_response_bytes=1)


def test_policy_leaves_the_stream_total_unset_unless_given() -> None:
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    assert bounds.max_total_stream_bytes is None
    assert (
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_stream_bytes=7,
        ).max_total_stream_bytes
        == 7
    )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1"])
def test_policy_rejects_an_invalid_stdin_total(value: int) -> None:
    with pytest.raises(ValueError):
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_stdin_bytes=value,
        )


def test_policy_leaves_the_stdin_total_unset_unless_given() -> None:
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    assert bounds.max_total_stdin_bytes is None
    assert (
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_stdin_bytes=7,
        ).max_total_stdin_bytes
        == 7
    )


class LaunchLimit(int):
    pass


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", LaunchLimit(1)])
def test_policy_rejects_an_invalid_archive_stderr_total(value: int) -> None:
    with pytest.raises(ValueError):
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_archive_stderr_bytes=value,
        )


def test_policy_leaves_archive_stderr_unbounded_unless_given() -> None:
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    assert bounds.max_total_archive_stderr_bytes is None
    assert (
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_archive_stderr_bytes=7,
        ).max_total_archive_stderr_bytes
        == 7
    )


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", LaunchLimit(1)])
def test_policy_rejects_an_invalid_aggregate_io_total(value: int) -> None:
    with pytest.raises(ValueError):
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_io_bytes=value,
        )


def test_policy_leaves_the_aggregate_io_total_unset_unless_given() -> None:
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    assert bounds.max_total_io_bytes is None
    assert (
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_io_bytes=7,
        ).max_total_io_bytes
        == 7
    )


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", LaunchLimit(1)])
def test_policy_rejects_an_invalid_launch_attempt_total(value: int) -> None:
    with pytest.raises(ValueError):
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_launch_attempts=value,
        )


def test_policy_leaves_launch_attempts_unbounded_unless_given() -> None:
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    assert bounds.max_total_launch_attempts is None
    assert (
        InspectionPolicy(
            max_references=1,
            max_argv_bytes=1,
            max_response_bytes=1,
            max_total_launch_attempts=7,
        ).max_total_launch_attempts
        == 7
    )


def test_request_does_not_resolve_executable_and_is_frozen() -> None:
    config = ClientConfig(client_call=["missing-inventory-engine"])
    request = InventoryRequest(
        kind="network", references=("net",), client_config=config
    )
    assert request.client_config is config
    with pytest.raises(FrozenInstanceError):
        request.__setattr__("kind", "image")


@pytest.mark.parametrize("kind", ["unsupported", "Network", ""])
def test_request_rejects_unknown_kind(kind: InventoryKind) -> None:
    with pytest.raises(ValueError):
        InventoryRequest(kind=kind, references=("net",), client_config=ClientConfig())


def test_capture_loads_engine_only_at_call_time() -> None:
    now = datetime.now(UTC)
    snapshot = InventorySnapshot(records=(), started_at=now, finished_at=now)
    engine = ModuleType("pywhales._inventory_capture")
    engine.__dict__["capture_inventory"] = lambda items, *, policy: snapshot
    bounds = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=1)
    with patch.dict("sys.modules", {engine.__name__: engine}):
        assert capture_inventory([], policy=bounds) is snapshot


@pytest.mark.parametrize("references", [(), ("",), (1,), ["network"]])
def test_request_rejects_invalid_references(references: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        InventoryRequest(
            kind="network", references=references, client_config=ClientConfig()
        )


def test_network_record_uses_network_inspect_model() -> None:
    model = NetworkInspectResult.model_validate({"Id": "id"})
    now = datetime.now(UTC)
    record = InventoryRecord(kind="network", id="id", payload=model)
    snapshot = InventorySnapshot(records=(record,), started_at=now, finished_at=now)
    assert NetworkInspectResult in get_args(InventoryPayload.__value__)
    assert snapshot.records[0].payload is model


def test_builder_is_an_inventory_kind_resource_and_payload() -> None:
    assert "builder" in get_args(InventoryKind.__value__)
    assert Builder in get_args(InventoryResource.__value__)
    assert BuilderInspectResult in get_args(InventoryPayload.__value__)
    request = InventoryRequest(
        kind="builder", references=("ci",), client_config=ClientConfig()
    )
    assert request.kind == "builder"


@pytest.mark.parametrize("kind", get_args(InventoryKind.__value__))
def test_every_kind_round_trips_through_dispatch(kind: InventoryKind) -> None:
    config = ClientConfig()
    resource = make_resource(kind, config, "identity")
    assert resource_kind(resource) == kind
    assert type(resource) in get_args(InventoryResource.__value__)
    assert resource.client_config is config


def test_builder_record_uses_builder_inspect_model() -> None:
    model = BuilderInspectResult.model_validate({"Name": "ci", "Driver": "docker"})
    now = datetime.now(UTC)
    record = InventoryRecord(kind="builder", id="ci", payload=model)
    snapshot = InventorySnapshot(records=(record,), started_at=now, finished_at=now)
    assert snapshot.records[0].payload is model
