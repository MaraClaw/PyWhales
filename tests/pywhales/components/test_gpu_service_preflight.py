"""Swarm GPU capacity preflight of `service create`; never launches Docker."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import override

import pytest

from pywhales import DockerClient
from pywhales.exceptions import GpuSupportError
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

SERVICE_ID = "svc0123456789"
NODE_A = "a" * 25
NODE_B = "b" * 25
NODE_LIST = ["node", "list", "--quiet"]
CREATE = ["service", "create", "--quiet", "--generic-resource", "gpu=2", "img"]

type Resource = dict[str, dict[str, str | int]]


def discrete(kind: str, value: int) -> Resource:
    return {"DiscreteResourceSpec": {"Kind": kind, "Value": value}}


def named(kind: str, value: str) -> Resource:
    return {"NamedResourceSpec": {"Kind": kind, "Value": value}}


def node_json(
    identity: str,
    *resources: Resource,
    availability: str = "active",
    state: str = "ready",
) -> dict[str, object]:
    """Return a node inspect object shaped like `jsons/nodes/1.json`."""
    return {
        "ID": identity,
        "Spec": {"Labels": {}, "Role": "worker", "Availability": availability},
        "Description": {
            "Hostname": f"host-{identity[0]}",
            "Resources": {
                "NanoCPUs": 4000000000,
                "MemoryBytes": 16481628160,
                "GenericResources": list(resources),
            },
        },
        "Status": {"State": state, "Addr": "172.31.0.109"},
    }


def swarm(
    tmp_path: Path, nodes: list[dict[str, object]]
) -> tuple[DockerClient, Path, list[str]]:
    """Return a fake-CLI client serving `nodes`, its call log and the inspect argv."""
    identities = [str(node["ID"]) for node in nodes]
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("node", "list"), stdout="\n".join(identities) + "\n"),
            Rule(match=("node", "inspect"), stdout=json.dumps(nodes)),
            Rule(match=("service", "create"), stdout=f"{SERVICE_ID}\n"),
        ],
    )
    return client, log, ["node", "inspect", *identities]


class OneShot(tuple[str, ...]):
    """A sequence of strings that refuses a second pass."""

    consumed = False

    @override
    def __iter__(self) -> Iterator[str]:
        if self.consumed:
            raise AssertionError("generic_resources was iterated twice")
        self.consumed = True
        return super().__iter__()


def test_refuses_when_no_single_node_holds_the_request(tmp_path: Path) -> None:
    client, log, inspect = swarm(
        tmp_path,
        [node_json(NODE_A, discrete("gpu", 1)), node_json(NODE_B, discrete("gpu", 1))],
    )

    with pytest.raises(GpuSupportError) as caught:
        client.service.create(
            "img", None, generic_resources=["gpu=2"], require_gpu=True
        )

    assert (caught.value.reason, caught.value.devices) == (
        "no_node_capacity",
        ("gpu=2",),
    )
    assert calls(log) == [NODE_LIST, inspect]


@pytest.mark.parametrize(
    "resources",
    [
        pytest.param(
            [named("gpu", "GPU-aaaa-1111"), named("gpu", "GPU-bbbb-2222")],
            id="named",
        ),
        pytest.param([discrete("gpu", 2)], id="discrete"),
    ],
)
def test_passes_with_a_ready_active_node_and_keeps_the_create_argv(
    tmp_path: Path, resources: list[Resource]
) -> None:
    client, log, inspect = swarm(
        tmp_path, [node_json(NODE_A, discrete("gpu", 1)), node_json(NODE_B, *resources)]
    )

    guarded = client.service.create(
        "img", None, generic_resources=["gpu=2"], require_gpu=True
    )
    plain = client.service.create("img", None, generic_resources=["gpu=2"])

    assert guarded.id == plain.id == SERVICE_ID
    assert calls(log) == [NODE_LIST, inspect, CREATE, CREATE]


def test_drained_node_with_capacity_is_ignored(tmp_path: Path) -> None:
    client, log, inspect = swarm(
        tmp_path,
        [
            node_json(NODE_A, discrete("gpu", 8), availability="drain"),
            node_json(NODE_B, discrete("gpu", 1)),
        ],
    )

    with pytest.raises(GpuSupportError) as caught:
        client.service.create(
            "img", None, generic_resources=["gpu=2"], require_gpu=True
        )

    assert caught.value.reason == "no_node_capacity"
    assert calls(log) == [NODE_LIST, inspect]


@pytest.mark.parametrize(
    "requests",
    [
        pytest.param((), id="empty"),
        pytest.param(["gpu=x"], id="malformed"),
        pytest.param(["gpu=0"], id="zero"),
    ],
)
def test_require_gpu_without_a_parseable_request_is_rejected(
    tmp_path: Path, requests: list[str]
) -> None:
    client, log, _ = swarm(tmp_path, [node_json(NODE_A, discrete("gpu", 2))])

    with pytest.raises(ValueError, match="require_gpu"):
        client.service.create("img", None, generic_resources=requests, require_gpu=True)

    assert calls(log) == []


@pytest.mark.parametrize("requests", [(), ["gpu=2"]], ids=["empty", "valid"])
def test_invalid_timeout_is_rejected_before_anything_else(
    tmp_path: Path, requests: list[str]
) -> None:
    client, log, _ = swarm(tmp_path, [node_json(NODE_A, discrete("gpu", 2))])

    with pytest.raises(ValueError, match="timeout") as caught:
        client.service.create(
            "img", None, generic_resources=requests, require_gpu=True, timeout=0.0
        )

    assert "require_gpu" not in str(caught.value)
    assert calls(log) == []


def test_default_create_does_not_probe_nodes(tmp_path: Path) -> None:
    client, log, _ = swarm(tmp_path, [node_json(NODE_A)])

    client.service.create("img", None, generic_resources=["gpu=2"])

    assert calls(log) == [CREATE]


def test_one_shot_requests_reach_both_the_probe_and_the_argv(tmp_path: Path) -> None:
    client, log, inspect = swarm(tmp_path, [node_json(NODE_A, discrete("gpu", 2))])

    client.service.create(
        "img", None, generic_resources=OneShot(["gpu=2"]), require_gpu=True
    )

    assert calls(log) == [NODE_LIST, inspect, CREATE]
