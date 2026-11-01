import json
from enum import StrEnum
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from pywhales import Node, Service, Task, enums
from pywhales.client_config import ClientConfig, ParsingError
from pywhales.components.node import models as node
from pywhales.components.service import models as service
from pywhales.components.system import models as system
from pywhales.components.task import models as task
from pywhales.utils import DockerCamelModel

ENUM_FIELDS: Final = (
    (node.NodeSpec, "role", enums.NodeRole.MANAGER),
    (node.NodeSpec, "availability", enums.NodeAvailability.DRAIN),
    (node.NodeStatus, "state", enums.NodeState.READY),
    (node.NodeManagerStatus, "reachability", enums.NodeReachability.REACHABLE),
    (task.TaskStatus, "state", enums.TaskState.RUNNING),
    (task.TaskInspectResult, "desired_state", enums.TaskState.SHUTDOWN),
    (task.RestartPolicy, "condition", enums.RestartCondition.ON_FAILURE),
    (task.ContainerSpec, "isolation", enums.Isolation.HYPERV),
    (service.ContainerSpec, "isolation", enums.Isolation.PROCESS),
    (service.ChangeConfig, "failure_action", enums.FailureAction.ROLLBACK),
    (service.ChangeConfig, "order", enums.UpdateOrder.START_FIRST),
    (service.EndpointPortConfig, "protocol", enums.NetworkProtocol.UDP),
    (service.EndpointPortConfig, "publish_mode", enums.PublishMode.HOST),
    (service.ServiceEndpointSpec, "mode", enums.EndpointMode.DNSRR),
    (service.ServiceUpdateStatus, "state", enums.ServiceUpdateState.ROLLBACK_STARTED),
    (system.SwarmInfo, "local_node_state", enums.SwarmState.LOCKED),
    (system.SystemInfo, "isolation", enums.Isolation.DEFAULT),
)


@pytest.mark.parametrize(("model_type", "field_name", "member"), ENUM_FIELDS)
@pytest.mark.parametrize("future", [False, True])
def test_output_preserves_string_when_known_or_future_value_is_parsed(
    model_type: type[DockerCamelModel], field_name: str, member: StrEnum, future: bool
) -> None:
    value = f"future-{member.value}" if future else member.value

    snapshot = model_type.model_validate({field_name: value})

    assert type(getattr(snapshot, field_name)) is str
    assert getattr(snapshot, field_name) == value


@pytest.mark.parametrize(("model_type", "field_name", "member"), ENUM_FIELDS)
def test_output_preserves_enum_when_explicit_member_is_supplied(
    model_type: type[DockerCamelModel], field_name: str, member: StrEnum
) -> None:
    payload = {field_name: member}

    snapshot = model_type.model_validate(payload)

    assert getattr(snapshot, field_name) is member
    assert snapshot.model_dump(mode="json", exclude_unset=True) == {
        field_name: member.value
    }


def test_node_preserves_aliases_when_nested_inspect_fields_are_parsed() -> None:
    payload = {
        "ID": "node-id",
        "Spec": {"Role": "future-role", "Availability": "future-availability"},
        "Description": {
            "Platform": {"OS": "linux"},
            "Resources": {"NanoCPUs": 4000000001},
        },
        "Status": {"State": "future-state"},
        "ManagerStatus": {"Reachability": "future-reachability"},
    }

    snapshot = node.NodeInspectResult.model_validate_json(json.dumps(payload))

    assert snapshot.id == "node-id"
    assert (
        snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload
    )


def test_service_preserves_mode_dictionary_when_extensions_are_present() -> None:
    payload = {
        "ID": "service-id",
        "Spec": {
            "Mode": {
                "Replicated": {"Replicas": 3, "Extension": [True, None, 2.5]},
                "FutureMode": {"Options": {"enabled": False}},
            },
            "UpdateConfig": {"FailureAction": "future-action", "Order": "future-order"},
        },
        "Endpoint": {
            "Spec": {"Mode": "future-endpoint"},
            "Ports": [{"Protocol": "future-protocol", "PublishMode": "future-publish"}],
        },
        "UpdateStatus": {"State": "future-state"},
    }

    snapshot = service.ServiceInspectResult.model_validate_json(json.dumps(payload))

    assert snapshot.spec is not None
    assert snapshot.spec.mode is not None
    replicated = snapshot.spec.mode["Replicated"]
    assert isinstance(replicated, dict)
    assert replicated["Replicas"] == 3
    assert (
        snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload
    )


def test_task_preserves_json_when_container_extensions_use_nested_shapes() -> None:
    payload = {
        "ID": "task-id",
        "ServiceID": "service-id",
        "NodeID": "node-id",
        "Spec": {
            "ContainerSpec": {
                "Privileges": {"Extension": {"values": [True, None, 1, 2.5]}},
                "Mounts": [{"Type": "vendor", "Extension": {"options": []}}],
                "HealthCheck": {"Extension": [1, {"enabled": False}]},
                "DnsConfig": {"Extension": {"servers": ["192.0.2.1"]}},
                "Secrets": [{"Extension": [None, True]}],
                "Configs": [{"Extension": {"value": 3}}],
                "Sysctls": {"Extension": ["value", 4]},
                "Isolation": "future-isolation",
                "TTY": True,
            }
        },
        "Status": {
            "State": "future-state",
            "ContainerStatus": {"ContainerID": "cid", "PID": 9},
        },
        "DesiredState": "future-desired-state",
    }

    snapshot = task.TaskInspectResult.model_validate_json(json.dumps(payload))

    assert (
        snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload
    )


def test_task_preserves_restart_dictionary_when_policy_has_extension_keys() -> None:
    payload = {
        "RestartPolicy": {"Condition": "future-condition", "Extension": [True, None]}
    }

    snapshot = task.TaskSpec.model_validate(payload)

    assert isinstance(snapshot.restart_policy, dict)
    assert snapshot.restart_policy["Condition"] == "future-condition"
    assert snapshot.model_dump(by_alias=True, exclude_unset=True) == payload


def test_system_preserves_json_when_registry_and_ca_have_extensions() -> None:
    payload = {
        "ID": "system-id",
        "Isolation": "",
        "Plugins": {"Authorization": [{"vendor": {"values": [True, None, 2.5]}}]},
        "RegistryConfig": {"Extension": {"weights": [1, 2.5], "enabled": False}},
        "Swarm": {
            "NodeID": "node-id",
            "LocalNodeState": "future-state",
            "Cluster": {
                "ID": "cluster-id",
                "Spec": {
                    "CAConfig": {"ExternalCA": [{"Extension": {"values": [1, None]}}]},
                    "TaskDefaults": {
                        "LogDriver": {"Options": {"vendor": {"enabled": True}}}
                    },
                },
            },
        },
    }

    snapshot = system.SystemInfo.model_validate_json(json.dumps(payload))

    assert type(snapshot.isolation) is str
    assert (
        snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload
    )


@pytest.mark.parametrize(
    "action", ["health_status: healthy", "exec_start: sh -c true", "future-action"]
)
def test_event_preserves_action_when_payload_has_json_attributes(action: str) -> None:
    payload = {
        "Type": "future-resource",
        "Action": action,
        "Actor": {
            "ID": "actor-id",
            "Attributes": {"vendor": {"values": [True, None, 2.5]}},
        },
        "timeNano": 123456789,
    }

    snapshot = system.DockerEvent.model_validate_json(json.dumps(payload))

    assert type(snapshot.action) is str
    assert (
        snapshot.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload
    )


@pytest.mark.parametrize(
    ("model_type", "field_name"),
    [
        (task.ContainerSpec, "Privileges"),
        (task.ContainerSpec, "Mounts"),
        (task.ContainerSpec, "HealthCheck"),
        (task.ContainerSpec, "DnsConfig"),
        (task.ContainerSpec, "Secrets"),
        (task.ContainerSpec, "Configs"),
        (task.ContainerSpec, "Sysctls"),
        (task.TaskSpec, "RestartPolicy"),
        (service.ServiceSpec, "Mode"),
        (system.DockerEventActor, "Attributes"),
        (system.Plugins, "Authorization"),
        (system.SwarmCAConfig, "ExternalCA"),
        (system.Driver, "Options"),
        (system.SystemInfo, "RegistryConfig"),
    ],
)
@pytest.mark.parametrize("invalid", [Path("not-json"), b"bytes", {1, 2}, (1, 2)])
def test_json_field_rejects_non_json_when_nested_in_extension(
    model_type: type[DockerCamelModel],
    field_name: str,
    invalid: Path | bytes | set[int] | tuple[int, int],
) -> None:
    nested = {"Extension": [invalid]}
    payload = {
        field_name: [nested]
        if field_name in {"Mounts", "Secrets", "Configs", "ExternalCA"}
        else nested
    }

    with pytest.raises(ValidationError) as failure:
        model_type.model_validate(payload)

    assert failure.value.errors()[0]["type"] == "invalid-json-value"


@pytest.mark.parametrize(
    "model_type",
    [
        node.NodeInspectResult,
        task.TaskInspectResult,
        service.ServiceInspectResult,
        system.SystemInfo,
    ],
)
def test_optional_fields_default_to_none_when_inspect_payload_is_empty(
    model_type: type[DockerCamelModel],
) -> None:
    payload = {}

    snapshot = model_type.model_validate(payload)

    assert snapshot.model_dump() == dict.fromkeys(model_type.model_fields, None)


def test_node_remains_mutable_when_role_is_reassigned() -> None:
    snapshot = node.NodeSpec.model_validate({"Role": "worker"})

    snapshot.role = enums.NodeRole.MANAGER

    assert snapshot.role is enums.NodeRole.MANAGER


@pytest.mark.parametrize("resource_type", [Node, Task, Service])
def test_fetch_rejects_malformed_inspect_output(
    monkeypatch: pytest.MonkeyPatch, resource_type: type[Node | Task | Service]
) -> None:
    config = ClientConfig()
    config._client_call_with_path = ["docker-test"]
    resource = resource_type(config, "resource-id", is_immutable_id=True)

    def malformed_output(_command: list[Path | str]) -> str:
        return "{"

    monkeypatch.setattr(f"{resource_type.__module__}.run", malformed_output)

    with pytest.raises(ParsingError):
        resource._fetch_inspect_result_json("resource-id")
