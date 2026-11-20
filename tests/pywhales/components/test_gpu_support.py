"""GPU probing of the selected daemon and run/create preflight; never launches Docker."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from pywhales import DockerClient, Node
from pywhales.components.node.models import NodeInspectResult
from pywhales.components.system.gpu import (
    GpuRequest,
    GpuSupport,
    cdi_device_kind,
    check_generic_resources,
    compose_gpu_requests,
    generic_resource_requests,
)
from pywhales.components.system.models import SystemInfo
from pywhales.exceptions import GpuSupportError, GpuUnavailable
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.argv_fakes import (
    Rule,
    calls,
    fake_cli_call,
    make_fake_cli,
)

INFO = ("system", "info", "--format", "{{json .}}")
GPU_INFO = json.dumps(
    {
        "OSType": "linux",
        "Runtimes": {"nvidia": {"path": "nvidia-container-runtime"}, "runc": {}},
        "CDISpecDirs": ["/etc/cdi", "/var/run/cdi"],
        "DiscoveredDevices": [
            {"Source": "cdi", "ID": "nvidia.com/gpu=0"},
            {"Source": "cdi", "ID": "nvidia.com/gpu=0:1"},
            {"Source": "cdi", "ID": "nvidia.com/gpu=all"},
        ],
    }
)
NO_GPU_INFO = json.dumps(
    {
        "OSType": "linux",
        "Runtimes": {"runc": {}},
        "CDISpecDirs": ["/etc/cdi"],
        "DiscoveredDevices": [{"Source": "cdi", "ID": "docker.com/gpu=webgpu"}],
    }
)


def support(
    os_type: str | None = "linux",
    runtimes: tuple[str, ...] = (),
    devices: tuple[str, ...] | None = (),
) -> GpuSupport:
    return GpuSupport(os_type, runtimes, (), devices)


def test_probe_targets_the_configured_daemon(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(tmp_path, [Rule(match=INFO, stdout=GPU_INFO)])
    client = DockerClient(client_call=client_call, context="gpu-host")

    result = client.system.gpu_support()

    assert calls(log) == [["--context", "gpu-host", *INFO]]
    assert result == GpuSupport(
        "linux",
        ("nvidia",),
        ("/etc/cdi", "/var/run/cdi"),
        ("nvidia.com/gpu=0", "nvidia.com/gpu=0:1", "nvidia.com/gpu=all"),
    )
    assert result.gpus_available


def test_wire_fixture_reports_vgpu_and_mig_devices() -> None:
    fixture = next(p for p in get_all_jsons("system_info") if p.name == "3.json")
    result = GpuSupport.from_info(SystemInfo(**json.loads(fixture.read_text())))

    assert result.gpu_runtimes == ("nvidia",)
    assert "nvidia.com/gpu=0:1" in result.gpu_devices
    result.check(
        gpus="all", devices=["nvidia.com/gpu=GPU-4316bf95-b805-cac2-c66f-adb71c20ec66"]
    )


def test_daemon_without_device_list_is_unknown_not_empty() -> None:
    result = GpuSupport.from_info(SystemInfo(OSType="linux"))

    assert result.cdi_devices is None
    assert not result.gpus_available


@pytest.mark.parametrize(
    ("device", "kind"),
    [
        ("nvidia.com/gpu=0", "nvidia.com/gpu"),
        ("nvidia.com/gpu=MIG-1b2c", "nvidia.com/gpu"),
        ("amd.com/gpu=all", "amd.com/gpu"),
        ("/dev/fuse", None),
        ("/dev/sda:/dev/xvdc:rwm", None),
        ("nvidia.com/gpu", None),
    ],
)
def test_cdi_device_kind(device: str, kind: str | None) -> None:
    assert cdi_device_kind(device) == kind


@pytest.mark.parametrize(
    ("gpu_support", "gpus"),
    [
        pytest.param(support(runtimes=("nvidia",)), "all", id="legacy-runtime"),
        pytest.param(support(devices=("amd.com/gpu=0",)), 1, id="amd-cdi"),
        pytest.param(support(os_type="windows"), None, id="no-request"),
    ],
)
def test_check_accepts_reported_gpu_paths(
    gpu_support: GpuSupport, gpus: int | str | None
) -> None:
    gpu_support.check(gpus=gpus, devices=["/dev/fuse"])


@pytest.mark.parametrize(
    ("gpu_support", "gpus", "devices", "reason", "missing"),
    [
        (support(os_type="windows"), "all", (), "not_linux", ()),
        (support(devices=("docker.com/gpu=webgpu",)), "all", (), "no_gpu_driver", ()),
        (
            support(devices=None),
            None,
            ("nvidia.com/gpu=0",),
            "cdi_devices_unreported",
            (),
        ),
        (
            support(devices=("nvidia.com/gpu=0",)),
            None,
            ("nvidia.com/gpu=0", "nvidia.com/gpu=1", "/dev/fuse"),
            "cdi_device_missing",
            ("nvidia.com/gpu=1",),
        ),
        (
            support(devices=("nvidia.com/gpu=0:1",)),
            None,
            ("nvidia.com/gpu=0:2",),
            "cdi_device_missing",
            ("nvidia.com/gpu=0:2",),
        ),
    ],
)
def test_check_refusals(
    gpu_support: GpuSupport,
    gpus: str | None,
    devices: tuple[str, ...],
    reason: str,
    missing: tuple[str, ...],
) -> None:
    with pytest.raises(GpuSupportError) as caught:
        gpu_support.check(gpus=gpus, devices=devices)

    assert (caught.value.reason, caught.value.devices) == (reason, missing)
    assert "nvidia.com/gpu=1" not in str(caught.value)


def test_run_refuses_before_image_preflight(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=INFO, stdout=NO_GPU_INFO)])

    with pytest.raises(GpuSupportError, match="no_gpu_driver"):
        client.run("cuda", gpus="all", require_gpu=True)

    assert calls(log) == [list(INFO)]


def test_create_checks_cdi_names_and_keeps_one_shot_devices(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=INFO, stdout=GPU_INFO), Rule(match=("create",), stdout="cid\n")],
    )

    client.container.create(
        "cuda",
        devices=iter(["nvidia.com/gpu=0:1"]),
        pull="never",
        require_gpu=True,
    )

    assert calls(log) == [
        list(INFO),
        ["create", "--device", "nvidia.com/gpu=0:1", "--pull", "never", "cuda"],
    ]


def test_require_gpu_without_a_gpu_request_is_rejected(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    with pytest.raises(ValueError, match="require_gpu"):
        client.run("cuda", devices=["/dev/fuse"], require_gpu=True)

    assert calls(log) == []


def test_default_run_does_not_probe(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)

    client.run("cuda", gpus="all", pull="never")

    assert calls(log) == [
        ["container", "run", "--gpus", "all", "--pull", "never", "cuda"]
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "docker: Error response from daemon: failed to discover GPU vendor from CDI: "
        "no known GPU vendor found\n",
        "docker: Error response from daemon: CDI device injection failed: "
        "unresolvable CDI devices nvidia.com/gpu=all\n",
        'docker: Error response from daemon: could not select device driver "" '
        "with capabilities: [[gpu]]\n",
    ],
)
def test_daemon_gpu_rejection_is_typed(tmp_path: Path, stderr: str) -> None:
    client, _ = make_fake_cli(
        tmp_path, [Rule(match=("run",), stderr=stderr, exit_code=125)]
    )

    with pytest.raises(GpuUnavailable):
        client.run("cuda", gpus="all", detach=True, pull="never")


INDEXED = ("nvidia.com/gpu=0", "nvidia.com/gpu=1")
MIG_INDEXES = ("nvidia.com/gpu=0:0", "nvidia.com/gpu=0:1")
UUIDS = ("nvidia.com/gpu=GPU-aaaa",)


def test_device_classification_keeps_reported_order() -> None:
    result = support(
        devices=(
            "nvidia.com/gpu=GPU-aaaa",
            "nvidia.com/gpu=1",
            "nvidia.com/gpu=0:1",
            "nvidia.com/gpu=all",
            "nvidia.com/gpu=MIG-bbbb",
            "amd.com/gpu=0",
            "nvidia.com/gpu=0",
            "docker.com/gpu=webgpu",
        )
    )

    assert result.whole_gpus == (
        "nvidia.com/gpu=1",
        "amd.com/gpu=0",
        "nvidia.com/gpu=0",
    )
    assert result.gpu_uuids == ("nvidia.com/gpu=GPU-aaaa",)
    assert result.mig_devices == ("nvidia.com/gpu=0:1", "nvidia.com/gpu=MIG-bbbb")
    assert result.gpu_count == 3


@pytest.mark.parametrize(
    ("devices", "count"),
    [
        pytest.param(None, None, id="unreported"),
        pytest.param((), 0, id="none-discovered"),
        pytest.param(("nvidia.com/gpu=gpu0", "nvidia.com/gpu=all"), 0, id="aliases"),
    ],
)
def test_gpu_count(devices: tuple[str, ...] | None, count: int | None) -> None:
    assert support(devices=devices).gpu_count == count


@pytest.mark.parametrize(
    ("gpus", "expected"),
    [
        pytest.param(2, GpuRequest(count=2), id="int"),
        pytest.param("all", GpuRequest(count=-1), id="bare-all"),
        pytest.param("count=3", GpuRequest(count=3), id="count"),
        pytest.param("count=all", GpuRequest(count=-1), id="count-all"),
        pytest.param('"device=0,2"', GpuRequest(device_ids=("0", "2")), id="devices"),
        pytest.param("device=GPU-x", GpuRequest(device_ids=("GPU-x",)), id="uuid"),
        pytest.param(
            "driver=nvidia,count=1",
            GpuRequest(count=1, driver="nvidia"),
            id="driver-and-count",
        ),
        pytest.param(
            "driver=nvidia", GpuRequest(count=1, driver="nvidia"), id="default"
        ),
        pytest.param(
            "capabilities=utility,options=x=y,count=2",
            GpuRequest(count=2),
            id="ignored-keys",
        ),
        pytest.param("count=x", None, id="bad-count"),
        pytest.param("foo=1", None, id="unknown-key"),
        pytest.param("count=1,count=2", None, id="duplicate-key"),
        pytest.param("2,3", GpuRequest(count=3), id="bare-counts-keep-the-last"),
        pytest.param("count=3,2", GpuRequest(count=2), id="keyword-then-bare-count"),
        pytest.param("2,2", None, id="same-bare-count-twice"),
        pytest.param("2,count=3", None, id="bare-count-then-keyword"),
        pytest.param("", None, id="empty"),
        pytest.param('"device=0', None, id="csv-error"),
    ],
)
def test_gpu_request_parse(gpus: int | str, expected: GpuRequest | None) -> None:
    assert GpuRequest.parse(gpus) == expected


@pytest.mark.parametrize(
    ("gpu_support", "gpus", "missing"),
    [
        pytest.param(
            support(devices=INDEXED),
            3,
            ("nvidia.com/gpu=2",),
            id="count-beyond-indexes",
        ),
        pytest.param(
            support(devices=INDEXED),
            10**12,
            ("nvidia.com/gpu=2",),
            id="huge-count-stays-bounded",
        ),
        pytest.param(
            support(devices=INDEXED),
            '"device=0,5"',
            ("nvidia.com/gpu=5",),
            id="index-missing",
        ),
        pytest.param(
            support(devices=UUIDS),
            "device=GPU-bbbb",
            ("nvidia.com/gpu=GPU-bbbb",),
            id="uuid-missing",
        ),
        pytest.param(
            support(devices=INDEXED),
            "driver=nvidia,device=nvidia.com/gpu=9",
            ("nvidia.com/gpu=9",),
            id="qualified-name",
        ),
        pytest.param(
            support(runtimes=("nvidia",), devices=("nvidia.com/gpu=0",)),
            2,
            ("nvidia.com/gpu=1",),
            id="count-with-runtime-registered",
        ),
    ],
)
def test_check_refuses_gpus_the_daemon_does_not_name(
    gpu_support: GpuSupport, gpus: int | str, missing: tuple[str, ...]
) -> None:
    with pytest.raises(GpuSupportError) as caught:
        gpu_support.check(gpus=gpus)

    assert (caught.value.reason, caught.value.devices) == (
        "gpu_device_missing",
        missing,
    )
    assert "nvidia.com/gpu" not in str(caught.value)


@pytest.mark.parametrize(
    ("gpu_support", "gpus"),
    [
        pytest.param(
            support(devices=("nvidia.com/gpu=0",)),
            "device=GPU-zzzz",
            id="uuid-without-uuid-names",
        ),
        pytest.param(support(runtimes=("nvidia",)), 4, id="count-on-runtime-only-host"),
        pytest.param(support(devices=INDEXED), "count=all", id="count-all"),
        pytest.param(support(devices=INDEXED), 2, id="count-reported"),
        pytest.param(support(devices=INDEXED), '"device=1,0"', id="indexes-reported"),
        pytest.param(support(devices=UUIDS), 2, id="count-on-uuid-only-daemon"),
        pytest.param(support(devices=INDEXED), "driver=amd,count=9", id="other-driver"),
        pytest.param(support(devices=INDEXED), "count=x", id="syntax-left-to-cli"),
        pytest.param(
            support(devices=("nvidia.com/gpu=gpu0",)), 4, id="type-index-alias"
        ),
        pytest.param(
            support(devices=("nvidia.com/gpu=0", *MIG_INDEXES)),
            "device=0:2",
            id="mig-index-unverified",
        ),
        pytest.param(
            support(devices=("nvidia.com/gpu=0", "nvidia.com/gpu=MIG-aaaa")),
            "device=MIG-bbbb",
            id="mig-uuid-unverified",
        ),
        pytest.param(
            support(devices=("nvidia.com/gpu=0:0",)),
            "device=1",
            id="whole-index-on-mig-only-daemon",
        ),
    ],
)
def test_check_accepts_gpus_it_cannot_disprove(
    gpu_support: GpuSupport, gpus: int | str
) -> None:
    gpu_support.check(gpus=gpus)


@pytest.mark.parametrize(
    ("gpu_support", "requests", "devices", "reason", "missing"),
    [
        pytest.param(
            support(os_type="windows"),
            [GpuRequest(count=1, driver="cdi")],
            (),
            "not_linux",
            (),
            id="not-linux",
        ),
        pytest.param(
            support(),
            [GpuRequest(count=1, driver="nvidia")],
            (),
            "no_gpu_driver",
            (),
            id="no-driver",
        ),
        pytest.param(
            support(devices=None, runtimes=("nvidia",)),
            [GpuRequest(driver="cdi", device_ids=("nvidia.com/gpu=0",))],
            (),
            "cdi_devices_unreported",
            (),
            id="cdi-unreported",
        ),
        pytest.param(
            support(devices=("nvidia.com/gpu=0",)),
            [
                GpuRequest(
                    driver="cdi", device_ids=("nvidia.com/gpu=3", "nvidia.com/gpu=0")
                )
            ],
            ("nvidia.com/gpu=9", "/dev/fuse"),
            "cdi_device_missing",
            ("nvidia.com/gpu=9", "nvidia.com/gpu=3"),
            id="cdi-missing-in-request-order",
        ),
        pytest.param(
            support(devices=INDEXED),
            [GpuRequest(count=3), GpuRequest(device_ids=("2", "7"))],
            (),
            "gpu_device_missing",
            ("nvidia.com/gpu=2", "nvidia.com/gpu=7"),
            id="resolved-names-deduplicated",
        ),
    ],
)
def test_check_requests_refusals(
    gpu_support: GpuSupport,
    requests: list[GpuRequest],
    devices: tuple[str, ...],
    reason: str,
    missing: tuple[str, ...],
) -> None:
    with pytest.raises(GpuSupportError) as caught:
        gpu_support.check_requests(requests, devices)

    assert (caught.value.reason, caught.value.devices) == (reason, missing)


def test_check_requests_materializes_iterables_and_skips_driver_check_for_cdi() -> None:
    gpu_support = support(devices=("docker.com/gpu=webgpu",))
    assert not gpu_support.gpus_available

    gpu_support.check_requests(
        (r for r in [GpuRequest(driver="cdi", device_ids=("docker.com/gpu=webgpu",))]),
        iter(["/dev/fuse"]),
    )
    gpu_support.check_requests([])


def test_run_refuses_unreported_gpu_count_before_image_preflight(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=INFO, stdout=GPU_INFO)])

    with pytest.raises(GpuSupportError) as caught:
        client.run("cuda", gpus=2, require_gpu=True)

    assert (caught.value.reason, caught.value.devices) == (
        "gpu_device_missing",
        ("nvidia.com/gpu=1",),
    )
    assert calls(log) == [list(INFO)]


COMPOSE_SERVICES: dict[str, JsonValue] = {
    "trainer": {
        "image": "cuda",
        "gpus": [{"count": 2, "driver": "nvidia"}, {"device_ids": ["GPU-aaaa"]}],
        "devices": [
            "/dev/fuse:/dev/fuse",
            {"source": "nvidia.com/gpu=0:1", "target": "nvidia.com/gpu=0:1"},
            {"source": "nvidia.com/gpu=all", "target": "nvidia.com/gpu=all"},
        ],
    },
    "serve": {
        "deploy": {
            "resources": {
                "reservations": {
                    "devices": [
                        {"capabilities": ["gpu"], "driver": "nvidia", "count": "all"},
                        {"capabilities": ["tpu"]},
                        {"capabilities": ["gpu"], "device_ids": ["1"]},
                        {
                            "capabilities": ["utility"],
                            "driver": "cdi",
                            "device_ids": ["nvidia.com/gpu=2"],
                        },
                    ]
                }
            }
        }
    },
    "all": {
        "gpus": "all",
        "devices": [
            {"source": "/dev/fuse", "target": "/dev/fuse", "permissions": "rwm"}
        ],
    },
    "plain": {"image": "alpine"},
    "odd": "not-a-service",
}


def test_compose_gpu_requests_reads_normalized_config() -> None:
    requests, names = compose_gpu_requests(COMPOSE_SERVICES)

    assert requests == (
        GpuRequest(count=2, driver="nvidia"),
        GpuRequest(device_ids=("GPU-aaaa",)),
        GpuRequest(count=-1, driver="nvidia"),
        GpuRequest(device_ids=("1",)),
        GpuRequest(device_ids=("nvidia.com/gpu=2",), driver="cdi"),
        GpuRequest(count=-1),
    )
    assert names == ("nvidia.com/gpu=0:1", "nvidia.com/gpu=all")


@pytest.mark.parametrize(
    ("capabilities", "counted"),
    [
        pytest.param(["compute", "utility"], True, id="nvidia-capabilities"),
        pytest.param(["amd"], True, id="amd-capability"),
        pytest.param([["video"]], True, id="nested-list"),
        pytest.param(["tpu"], False, id="not-a-gpu-capability"),
    ],
)
def test_compose_gpu_requests_reads_gpu_capabilities(
    capabilities: JsonValue, counted: bool
) -> None:
    services: dict[str, JsonValue] = {
        "a": {
            "deploy": {
                "resources": {
                    "reservations": {
                        "devices": [{"capabilities": capabilities, "count": 1}]
                    }
                }
            }
        }
    }

    expected = ((GpuRequest(count=1),), ()) if counted else ((), ())
    assert compose_gpu_requests(services) == expected


def test_compose_gpu_requests_skips_unexpected_shapes() -> None:
    services: dict[str, JsonValue] = {
        "a": {
            "gpus": [1, {"count": True}, {"count": "2"}, {"device_ids": "0"}],
            "devices": [
                1,
                None,
                "nvidia.com/gpu=0",
                {"source": 3},
                {"target": "nvidia.com/gpu=0"},
                {"source": "nvidia.com/gpu=0", "target": "/dev/gpu0"},
            ],
            "deploy": {"resources": []},
        },
        "b": {"deploy": {"resources": {"reservations": {"devices": "gpu"}}}},
        "c": None,
    }

    assert compose_gpu_requests(services) == ((), ())


def test_compose_requests_feed_the_preflight() -> None:
    requests, names = compose_gpu_requests(COMPOSE_SERVICES)

    with pytest.raises(GpuSupportError) as caught:
        support(devices=("nvidia.com/gpu=all", "nvidia.com/gpu=0")).check_requests(
            requests, names
        )

    assert (caught.value.reason, caught.value.devices) == (
        "cdi_device_missing",
        ("nvidia.com/gpu=0:1", "nvidia.com/gpu=2"),
    )


@pytest.mark.parametrize(
    ("requests", "expected"),
    [
        pytest.param(["gpu=+2"], {"gpu": 2}, id="leading-plus"),
        pytest.param(["gpu=-1", "gpu=++1", "gpu=+", "gpu=0"], {}, id="rejected"),
    ],
)
def test_generic_resource_requests(
    requests: list[str], expected: dict[str, int]
) -> None:
    assert generic_resource_requests(requests) == expected


def node(
    state: str = "ready",
    availability: str = "active",
    resources: list[dict[str, dict[str, str | int]]] | None = None,
) -> Node:
    """Build a Node handle pinned to a snapshot, so reads never launch Docker."""
    handle = Node(DockerClient().client_config, "n" * 25, is_immutable_id=True)
    handle._set_pinned_inspect_result(
        NodeInspectResult.model_validate(
            {
                "ID": "n" * 25,
                "Spec": {"Availability": availability},
                "Status": {"State": state},
                "Description": {"Resources": {"GenericResources": resources or []}},
            }
        )
    )
    return handle


def discrete(kind: str, value: int) -> dict[str, dict[str, str | int]]:
    return {"DiscreteResourceSpec": {"Kind": kind, "Value": value}}


def named(kind: str, value: str) -> dict[str, dict[str, str | int]]:
    return {"NamedResourceSpec": {"Kind": kind, "Value": value}}


@pytest.mark.parametrize(
    ("requests", "nodes"),
    [
        pytest.param(["gpu=2"], [node(resources=[discrete("gpu", 4)])], id="discrete"),
        pytest.param(
            ["gpu=2"],
            [node(resources=[named("gpu", "gpu-0"), named("gpu", "gpu-1")])],
            id="named-count",
        ),
        pytest.param(
            ["gpu=3", "fpga=1"],
            [
                node(resources=[discrete("gpu", 1)]),
                node(
                    resources=[
                        discrete("gpu", 2),
                        named("gpu", "g"),
                        discrete("fpga", 1),
                    ]
                ),
            ],
            id="one-node-serves-all",
        ),
        pytest.param(
            ["gpu", "=1", "gpu=x", "gpu=-1", "gpu=1.5", "", "gpu=0", "gpu=0"],
            [],
            id="malformed-and-zero-skipped",
        ),
        pytest.param(
            [" gpu = 2 "], [node(resources=[discrete("gpu", 2)])], id="stripped"
        ),
        pytest.param(
            ["gpu=+2"], [node(resources=[discrete("gpu", 2)])], id="leading-plus"
        ),
    ],
)
def test_check_generic_resources_accepts(
    requests: list[str], nodes: list[Node]
) -> None:
    check_generic_resources(iter(requests), iter(nodes))


@pytest.mark.parametrize(
    ("requests", "nodes", "unsatisfied"),
    [
        pytest.param(
            ["gpu=2"],
            [
                node(availability="drain", resources=[discrete("gpu", 8)]),
                node(availability="pause", resources=[discrete("gpu", 8)]),
                node(state="down", resources=[discrete("gpu", 8)]),
                node(resources=[discrete("gpu", 1)]),
            ],
            ("gpu=2",),
            id="ineligible-nodes-ignored",
        ),
        pytest.param(
            ["gpu=3"],
            [node(resources=[named("gpu", "gpu-0"), named("gpu", "gpu-1")])],
            ("gpu=3",),
            id="named-count-short",
        ),
        pytest.param(
            ["gpu=2", "fpga=1"],
            [
                node(resources=[discrete("gpu", 2)]),
                node(resources=[discrete("fpga", 1)]),
            ],
            ("gpu=2", "fpga=1"),
            id="joint-shortfall-lists-all",
        ),
        pytest.param(
            ["gpu=2", "fpga=1"],
            [node(resources=[discrete("gpu", 2)])],
            ("fpga=1",),
            id="individual-shortfall",
        ),
        pytest.param(
            ["gpu=1", "gpu=1", "gpu=x"],
            [node(resources=[discrete("gpu", 1)])],
            ("gpu=2",),
            id="duplicates-summed",
        ),
        pytest.param(
            ["gpu=+2"],
            [node(resources=[discrete("gpu", 1)])],
            ("gpu=2",),
            id="leading-plus",
        ),
        pytest.param(["gpu=1"], [], ("gpu=1",), id="no-nodes"),
        pytest.param(["gpu=1"], [node()], ("gpu=1",), id="no-resources"),
    ],
)
def test_check_generic_resources_refuses(
    requests: list[str], nodes: list[Node], unsatisfied: tuple[str, ...]
) -> None:
    with pytest.raises(GpuSupportError) as caught:
        check_generic_resources(requests, nodes)

    assert (caught.value.reason, caught.value.devices) == (
        "no_node_capacity",
        unsatisfied,
    )
