"""GPU probing of the selected daemon and run/create preflight; never launches Docker."""

import json
from pathlib import Path

import pytest

from pywhales import DockerClient
from pywhales.components.system.gpu import GpuSupport, cdi_device_kind
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
