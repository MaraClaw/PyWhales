"""Compose `require_gpu` preflight with canned CLI output; never launches Docker.

A recording fake CLI answers `compose config` and `system info`, so the tests
assert exactly which launches happen and in what order.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from pywhales import DockerClient
from pywhales.exceptions import GpuSupportError
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli

CONFIG = ["compose", "config", "--format", "json"]
INFO = ["system", "info", "--format", "{{json .}}"]
RESERVATION: JsonValue = {
    "resources": {"reservations": {"devices": [{"capabilities": ["gpu"], "count": 1}]}}
}


def config(*services: str) -> list[str]:
    """Return the argv of the `compose config` probe that names `services`."""
    return [*CONFIG, *services]


def device(source: str, target: str | None = None) -> dict[str, JsonValue]:
    """Return a normalized Compose `devices` mapping; `target` defaults to `source`."""
    return {"source": source, "target": target or source, "permissions": "rwm"}


def fake_project(
    tmp_path: Path,
    services: Mapping[str, JsonValue],
    *devices: str,
    runtimes: tuple[str, ...] = ("nvidia",),
) -> tuple[DockerClient, Path]:
    """Return a client whose `compose config` and `system info` are canned.

    Args:
        tmp_path: Directory receiving the fake CLI and its call log.
        services: The `services` mapping of the normalized project.
        devices: CDI names the daemon reports as discovered.
        runtimes: Runtime names the daemon registers besides `runc`.
    """
    info = {
        "OSType": "linux",
        "Runtimes": {name: {} for name in (*runtimes, "runc")},
        "CDISpecDirs": ["/etc/cdi"],
        "DiscoveredDevices": [{"Source": "cdi", "ID": device} for device in devices],
    }
    project = {"name": "demo", "services": services}
    return make_fake_cli(
        tmp_path,
        [
            Rule(match=CONFIG, stdout=json.dumps(project)),
            Rule(match=INFO, stdout=json.dumps(info)),
        ],
    )


def test_up_refuses_missing_gpus_before_launching(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path, {"trainer": {"gpus": [{"count": 2}]}}, "nvidia.com/gpu=0"
    )

    with pytest.raises(GpuSupportError) as caught:
        client.compose.up(require_gpu=True)

    assert (caught.value.reason, caught.value.devices) == (
        "gpu_device_missing",
        ("nvidia.com/gpu=1",),
    )
    assert calls(log) == [CONFIG, INFO]


@pytest.mark.parametrize(
    ("services", "selected"),
    [
        pytest.param(None, [], id="all"),
        pytest.param("trainer", ["trainer"], id="string"),
        pytest.param(["trainer"], ["trainer"], id="list"),
        pytest.param(["trainer", "web"], ["trainer", "web"], id="several"),
    ],
)
def test_up_names_the_selected_services_in_the_config_probe(
    tmp_path: Path, services: list[str] | str | None, selected: list[str]
) -> None:
    client, log = fake_project(
        tmp_path,
        {"trainer": {"deploy": RESERVATION}, "web": {}},
        "nvidia.com/gpu=0",
    )

    client.compose.up(services, require_gpu=True)

    assert calls(log) == [config(*selected), INFO, ["compose", "up", *selected]]


DEPENDS_ON_FORMS = [
    pytest.param({"worker": {"condition": "service_started"}}, id="mapping"),
    pytest.param(["worker"], id="list"),
]


def web_on_gpu_worker(depends_on: JsonValue) -> dict[str, JsonValue]:
    """Return a GPU-free service that depends on a service requesting one GPU."""
    return {
        "web": {"depends_on": depends_on},
        "worker": {"gpus": [{"count": 1}]},
    }


@pytest.mark.parametrize("depends_on", DEPENDS_ON_FORMS)
def test_up_passes_when_the_daemon_serves_a_dependency(
    tmp_path: Path, depends_on: JsonValue
) -> None:
    client, log = fake_project(
        tmp_path, web_on_gpu_worker(depends_on), "nvidia.com/gpu=0"
    )

    client.compose.up(["web"], require_gpu=True)

    assert calls(log) == [config("web"), INFO, ["compose", "up", "web"]]


@pytest.mark.parametrize("depends_on", DEPENDS_ON_FORMS)
def test_up_refuses_when_the_daemon_cannot_serve_a_dependency(
    tmp_path: Path, depends_on: JsonValue
) -> None:
    client, log = fake_project(tmp_path, web_on_gpu_worker(depends_on), runtimes=())

    with pytest.raises(GpuSupportError) as caught:
        client.compose.up(["web"], require_gpu=True)

    assert caught.value.reason == "no_gpu_driver"
    assert calls(log) == [config("web"), INFO]


def test_up_follows_dependencies_transitively(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {
            "web": {"depends_on": {"api": {"condition": "service_started"}}},
            "api": {"depends_on": ["worker"]},
            "worker": {"gpus": "all"},
            "unrelated": {"gpus": [{"count": 8}]},
        },
        "nvidia.com/gpu=0",
    )

    client.compose.up("web", require_gpu=True)

    assert calls(log) == [config("web"), INFO, ["compose", "up", "web"]]


def test_up_without_dependencies_ignores_them(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {
            "web": {"depends_on": {"worker": {"condition": "service_started"}}},
            "worker": {"gpus": [{"count": 1}]},
        },
        "nvidia.com/gpu=0",
    )

    with pytest.raises(ValueError, match="require_gpu found no GPU request"):
        client.compose.up(["web"], dependencies=False, require_gpu=True)

    assert calls(log) == [config("web")]


def test_up_without_services_in_the_configuration_finds_no_request(
    tmp_path: Path,
) -> None:
    client, log = fake_project(tmp_path, {})

    with pytest.raises(ValueError, match="require_gpu found no GPU request"):
        client.compose.up(require_gpu=True)

    assert calls(log) == [CONFIG]


def test_up_with_no_services_is_a_no_op_without_a_probe(tmp_path: Path) -> None:
    client, log = fake_project(tmp_path, {"trainer": {"gpus": "all"}})

    client.compose.up([], require_gpu=True)

    assert calls(log) == []


@pytest.mark.parametrize(
    "idle",
    [
        pytest.param({"scale": 0}, id="scale"),
        pytest.param({"deploy": {"replicas": 0}}, id="replicas"),
    ],
)
def test_up_skips_services_scaled_to_zero(
    tmp_path: Path, idle: dict[str, JsonValue]
) -> None:
    client, log = fake_project(
        tmp_path,
        {"idle": {"gpus": [{"count": 4}], **idle}, "web": {"gpus": [{"count": 1}]}},
        "nvidia.com/gpu=0",
    )

    client.compose.up(require_gpu=True)

    assert calls(log) == [CONFIG, INFO, ["compose", "up"]]


def test_up_scales_can_bring_a_scaled_to_zero_service_back(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {"idle": {"scale": 0, "gpus": [{"count": 2}]}},
        "nvidia.com/gpu=0",
    )

    with pytest.raises(GpuSupportError) as caught:
        client.compose.up(scales={"idle": 1}, require_gpu=True)

    assert caught.value.devices == ("nvidia.com/gpu=1",)
    assert calls(log) == [CONFIG, INFO]


def test_up_scales_can_scale_a_service_down_to_zero(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {"busy": {"gpus": [{"count": 2}]}, "web": {"gpus": [{"count": 1}]}},
        "nvidia.com/gpu=0",
    )

    client.compose.up(scales={"busy": 0}, require_gpu=True)

    assert calls(log) == [CONFIG, INFO, ["compose", "up", "--scale", "busy=0"]]


def test_up_checks_what_a_scaled_to_zero_service_depends_on(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {
            "idle": {"scale": 0, "depends_on": ["worker"]},
            "worker": {"gpus": [{"count": 2}]},
        },
        "nvidia.com/gpu=0",
    )

    with pytest.raises(GpuSupportError) as caught:
        client.compose.up(["idle"], require_gpu=True)

    assert caught.value.devices == ("nvidia.com/gpu=1",)
    assert calls(log) == [config("idle"), INFO]


def test_run_refuses_a_missing_cdi_device_before_launching(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {"trainer": {"devices": [device("nvidia.com/gpu=0:1"), device("/dev/fuse")]}},
        "nvidia.com/gpu=0",
    )

    with pytest.raises(GpuSupportError) as caught:
        client.compose.run("trainer", ["nvidia-smi"], require_gpu=True, tty=False)

    assert (caught.value.reason, caught.value.devices) == (
        "cdi_device_missing",
        ("nvidia.com/gpu=0:1",),
    )
    assert calls(log) == [config("trainer"), INFO]


def test_run_launches_the_unchanged_argv_after_a_passing_probe(
    tmp_path: Path,
) -> None:
    client, log = fake_project(
        tmp_path,
        {"trainer": {"devices": [device("nvidia.com/gpu=0:1")]}},
        "nvidia.com/gpu=0:1",
    )

    client.compose.run("trainer", ["nvidia-smi"], require_gpu=True, tty=False)

    assert calls(log) == [
        config("trainer"),
        INFO,
        ["compose", "run", "--no-tty", "trainer", "nvidia-smi"],
    ]


def test_run_does_not_skip_a_service_scaled_to_zero(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {"trainer": {"scale": 0, "gpus": [{"count": 2}]}},
        "nvidia.com/gpu=0",
    )

    with pytest.raises(GpuSupportError) as caught:
        client.compose.run("trainer", require_gpu=True, tty=False)

    assert caught.value.devices == ("nvidia.com/gpu=1",)
    assert calls(log) == [config("trainer"), INFO]


def test_run_skips_a_dependency_scaled_to_zero(tmp_path: Path) -> None:
    client, log = fake_project(
        tmp_path,
        {
            "trainer": {"gpus": [{"count": 1}], "depends_on": ["idle"]},
            "idle": {"scale": 0, "gpus": [{"count": 2}]},
        },
        "nvidia.com/gpu=0",
    )

    client.compose.run("trainer", require_gpu=True, tty=False)

    assert calls(log) == [
        config("trainer"),
        INFO,
        ["compose", "run", "--no-tty", "trainer"],
    ]


@pytest.mark.parametrize(
    ("timeout", "stream"),
    [
        pytest.param(-1, False, id="timeout"),
        pytest.param(None, True, id="tty-with-stream"),
    ],
)
def test_run_validates_before_the_preflight(
    tmp_path: Path, timeout: float | None, stream: bool
) -> None:
    client, log = fake_project(tmp_path, {"trainer": {"gpus": "all"}})

    with pytest.raises(ValueError):
        client.compose.run("trainer", stream=stream, timeout=timeout, require_gpu=True)

    assert calls(log) == []


def test_run_encodes_its_arguments_before_the_preflight(tmp_path: Path) -> None:
    client, log = fake_project(tmp_path, {"trainer": {"gpus": "all"}})
    client.client_config.strict_env_transport = True

    with pytest.raises(ValueError):
        client.compose.run(
            "trainer", envs={"PYTHON_VERSION": "3"}, require_gpu=True, tty=False
        )

    assert calls(log) == []


def test_default_calls_make_no_config_or_info_launches(tmp_path: Path) -> None:
    client, log = fake_project(tmp_path, {"trainer": {"gpus": "all"}})

    client.compose.up()
    client.compose.run("trainer", tty=False)

    assert calls(log) == [["compose", "up"], ["compose", "run", "--no-tty", "trainer"]]
