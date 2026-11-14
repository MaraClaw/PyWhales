"""Service create, update, inspect and ps argv, exercised through a recording fake CLI."""

import json
from collections.abc import Callable
from datetime import timedelta
from inspect import Parameter, signature
from pathlib import Path

import pytest

from pywhales import DockerClient
from pywhales.client_config import ClientNotFoundError
from pywhales.components.service import _spec_options
from pywhales.components.service.cli_wrapper import Service, ServiceCLI
from pywhales.components.service.models import ServiceInspectResult
from pywhales.exceptions import DockerException
from tests.pywhales.components.argv_fakes import Rule, calls, make_fake_cli, records

SERVICE_ID = "svc0123456789"
MISSING = ["missing-pywhales-test-executable"]


@pytest.fixture
def fake(tmp_path: Path) -> tuple[DockerClient, Path]:
    return make_fake_cli(
        tmp_path,
        [
            Rule(match=("create",), stdout=f"{SERVICE_ID}\n", env=["E1_APP_TOKEN"]),
            Rule(match=("update",), env=["E1_APP_TOKEN"]),
            Rule(match=("ps",), stdout="task-one\ntask-two\n"),
            Rule(match=("inspect", "--pretty"), stdout="ID: x\nName: web\n"),
            Rule(
                match=("inspect",),
                stdout=json.dumps([{"ID": SERVICE_ID, "Spec": {"Name": "web"}}]),
            ),
        ],
    )


def test_legacy_create_minimal(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    service = client.service.create("img", None)
    assert service.id == SERVICE_ID
    assert calls(log) == [["service", "create", "--quiet", "img"]]


def test_legacy_create_keyword_options_keep_argv_order(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.create(
        "img",
        ["cmd", "arg"],
        cap_add=["NET_ADMIN", "SYS_TIME"],
        cap_drop="MKNOD",
        constraints=["node.role==worker"],
        detach=True,
        dns=["1.1.1.1"],
        dns_options=["ndots:2"],
        dns_search=["example.com"],
        endpoint_mode="dnsrr",
        entrypoint="/entry",
        envs={"E1_APP_TOKEN": "secret value"},
        env_files=["a.env", Path("b.env")],
        generic_resources=["gpu=2"],
        groups=["wheel"],
        healthcheck=False,
        health_cmd="echo ok",
        health_interval=5,
        health_retries=3,
        health_start_period=7,
        health_timeout=9,
        hostname="host",
        init=True,
        isolation="process",
        labels={"k": "v", "k2": "v2"},
        limit_cpu=0.5,
        limit_memory="64m",
        limit_pids=100,
        log_driver="json-file",
        network="net",
        restart_condition="on-failure",
        restart_max_attempts=4,
        secrets=[{"source": "s", "target": "t"}],
        mounts=[{"type": "volume", "source": "m", "target": "/m"}],
    )
    assert calls(log) == [
        [
            "service",
            "create",
            "--quiet",
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "SYS_TIME",
            "--cap-drop",
            "MKNOD",
            "--constraint",
            "node.role==worker",
            "--detach",
            "--dns",
            "1.1.1.1",
            "--dns-option",
            "ndots:2",
            "--dns-search",
            "example.com",
            "--endpoint-mode",
            "dnsrr",
            "--entrypoint",
            "/entry",
            "--env",
            "E1_APP_TOKEN",
            "--env-file",
            "a.env",
            "--env-file",
            "b.env",
            "--generic-resource",
            "gpu=2",
            "--group",
            "wheel",
            "--no-healthcheck",
            "--health-cmd",
            "echo ok",
            "--health-interval",
            "5s",
            "--health-retries",
            "3",
            "--health-start-period",
            "7s",
            "--health-timeout",
            "9s",
            "--hostname",
            "host",
            "--init",
            "--isolation",
            "process",
            "--label",
            "k=v",
            "--label",
            "k2=v2",
            "--limit-cpu",
            "0.5",
            "--limit-memory",
            "64m",
            "--limit-pids",
            "100",
            "--log-driver",
            "json-file",
            "--restart-condition",
            "on-failure",
            "--restart-max-attempts",
            "4",
            "--mount",
            "type=volume,source=m,target=/m",
            "--network",
            "net",
            "--secret",
            "source=s,target=t",
            "img",
            "cmd",
            "arg",
        ]
    ]
    assert records(log)[0]["env"] == {"E1_APP_TOKEN": "secret value"}


def test_legacy_create_positional_binding(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    client.service.create(
        "img",
        ["run"],
        ["A"],
        ["B"],
        ["c=1"],
        True,
        ["8.8.8.8"],
        ["o"],
        ["s.example"],
        "vip",
        "/e",
        {},
        [],
        ["r=1"],
        ["g"],
        False,
        "hc",
        1,
        2,
        3,
        4,
        {},
        "h",
        True,
        "iso",
        {"l": "1"},
        1.5,
        "1g",
        7,
        "drv",
        "n",
        "any",
        8,
        [{"source": "x"}],
        [{"target": "/y"}],
    )
    assert calls(log) == [
        [
            "service",
            "create",
            "--quiet",
            "--cap-add",
            "A",
            "--cap-drop",
            "B",
            "--constraint",
            "c=1",
            "--detach",
            "--dns",
            "8.8.8.8",
            "--dns-option",
            "o",
            "--dns-search",
            "s.example",
            "--endpoint-mode",
            "vip",
            "--entrypoint",
            "/e",
            "--generic-resource",
            "r=1",
            "--group",
            "g",
            "--no-healthcheck",
            "--health-cmd",
            "hc",
            "--health-interval",
            "1s",
            "--health-retries",
            "2",
            "--health-start-period",
            "3s",
            "--health-timeout",
            "4s",
            "--hostname",
            "h",
            "--init",
            "--isolation",
            "iso",
            "--label",
            "l=1",
            "--limit-cpu",
            "1.5",
            "--limit-memory",
            "1g",
            "--limit-pids",
            "7",
            "--log-driver",
            "drv",
            "--restart-condition",
            "any",
            "--restart-max-attempts",
            "8",
            "--mount",
            "target=/y",
            "--network",
            "n",
            "--secret",
            "source=x",
            "img",
            "run",
        ]
    ]


@pytest.mark.parametrize(
    ("selection", "expected"),
    [("web", ["web"]), (["a", "b"], ["a", "b"])],
)
def test_legacy_ps_argv_and_handles(
    fake: tuple[DockerClient, Path], selection: str | list[str], expected: list[str]
) -> None:
    client, log = fake
    tasks = client.service.ps(selection)
    assert [task.id for task in tasks] == ["task-one", "task-two"]
    assert calls(log) == [["service", "ps", "--quiet", "--no-trunc", *expected]]


def test_legacy_ps_empty_selection_does_not_launch(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    assert client.service.ps([]) == []
    assert calls(log) == []


def test_legacy_create_hosts_mapping_builds_host_options(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.create("img", None, hosts={"db": "10.0.0.5", "cache": "10.0.0.6"})
    assert calls(log) == [
        [
            "service",
            "create",
            "--quiet",
            "--host",
            "db:10.0.0.5",
            "--host",
            "cache:10.0.0.6",
            "img",
        ]
    ]


type Case = tuple[dict[str, object], list[str]]

CREATE_CASES: dict[str, Case] = {
    "config": (
        {"config": [{"source": "c", "target": "/c", "mode": "0440"}, {"source": "d"}]},
        ["--config", "source=c,target=/c,mode=0440", "--config", "source=d"],
    ),
    "container-label": (
        {"container_labels": {"a": "b", "c": "d"}},
        ["--container-label", "a=b", "--container-label", "c=d"],
    ),
    "credential-spec": (
        {"credential_spec": "file://spec.json"},
        ["--credential-spec", "file://spec.json"],
    ),
    "health-start-interval": (
        {"health_start_interval": 2},
        ["--health-start-interval", "2s"],
    ),
    "log-opt": (
        {"log_opts": {"max-size": "1m", "max-file": "3"}},
        ["--log-opt", "max-size=1m", "--log-opt", "max-file=3"],
    ),
    "max-concurrent": ({"max_concurrent": 3}, ["--max-concurrent", "3"]),
    "memory-swap-forwarded-verbatim": ({"memory_swap": -1}, ["--memory-swap", "-1"]),
    "memory-swap-text": ({"memory_swap": "2g"}, ["--memory-swap", "2g"]),
    "memory-swappiness-zero": ({"memory_swappiness": 0}, ["--memory-swappiness", "0"]),
    "mode-replicated": ({"mode": "replicated"}, ["--mode", "replicated"]),
    "mode-global": ({"mode": "global"}, ["--mode", "global"]),
    "mode-replicated-job": ({"mode": "replicated-job"}, ["--mode", "replicated-job"]),
    "mode-global-job": ({"mode": "global-job"}, ["--mode", "global-job"]),
    "name": ({"name": "web"}, ["--name", "web"]),
    "no-resolve-image": ({"no_resolve_image": True}, ["--no-resolve-image"]),
    "oom-score-adj": ({"oom_score_adj": -500}, ["--oom-score-adj", "-500"]),
    "placement-pref": (
        {"placement_prefs": ["spread=node.labels.zone", "spread=node.labels.rack"]},
        [
            "--placement-pref",
            "spread=node.labels.zone",
            "--placement-pref",
            "spread=node.labels.rack",
        ],
    ),
    "publish-long": (
        {
            "publish": [
                {"published": 8080, "target": 80, "protocol": "tcp", "mode": "host"}
            ]
        },
        ["--publish", "published=8080,target=80,protocol=tcp,mode=host"],
    ),
    "read-only": ({"read_only": True}, ["--read-only"]),
    "replicas-zero": ({"replicas": 0}, ["--replicas", "0"]),
    "replicas-max-per-node": (
        {"replicas_max_per_node": 2},
        ["--replicas-max-per-node", "2"],
    ),
    "reserve-cpu": ({"reserve_cpu": 0.25}, ["--reserve-cpu", "0.25"]),
    "reserve-memory": ({"reserve_memory": "64m"}, ["--reserve-memory", "64m"]),
    "restart-delay": ({"restart_delay": 5}, ["--restart-delay", "5000000000ns"]),
    "restart-window": ({"restart_window": 60}, ["--restart-window", "60000000000ns"]),
    "stop-grace-period": (
        {"stop_grace_period": 3},
        ["--stop-grace-period", "3000000000ns"],
    ),
    "stop-signal": ({"stop_signal": "SIGUSR1"}, ["--stop-signal", "SIGUSR1"]),
    "sysctl": (
        {"sysctls": {"net.core.somaxconn": "1024", "kernel.shmmax": "1"}},
        ["--sysctl", "net.core.somaxconn=1024", "--sysctl", "kernel.shmmax=1"],
    ),
    "tty": ({"tty": True}, ["--tty"]),
    "ulimit": (
        {"ulimits": ["nofile=1024:2048", "nproc=64"]},
        ["--ulimit", "nofile=1024:2048", "--ulimit", "nproc=64"],
    ),
    "user": ({"user": "1000:1000"}, ["--user", "1000:1000"]),
    "with-registry-auth": ({"with_registry_auth": True}, ["--with-registry-auth"]),
    "workdir": ({"workdir": "/srv"}, ["--workdir", "/srv"]),
    "update-policy": (
        {
            "update_parallelism": 0,
            "update_delay": 2,
            "update_failure_action": "rollback",
            "update_monitor": 3,
            "update_max_failure_ratio": 0.5,
            "update_order": "start-first",
        },
        [
            "--update-parallelism",
            "0",
            "--update-delay",
            "2000000000ns",
            "--update-failure-action",
            "rollback",
            "--update-monitor",
            "3000000000ns",
            "--update-max-failure-ratio",
            "0.5",
            "--update-order",
            "start-first",
        ],
    ),
    "rollback-policy": (
        {
            "rollback_parallelism": 4,
            "rollback_delay": 5,
            "rollback_failure_action": "continue",
            "rollback_monitor": 6,
            "rollback_max_failure_ratio": 0.0,
            "rollback_order": "stop-first",
        },
        [
            "--rollback-parallelism",
            "4",
            "--rollback-delay",
            "5000000000ns",
            "--rollback-failure-action",
            "continue",
            "--rollback-monitor",
            "6000000000ns",
            "--rollback-max-failure-ratio",
            "0.0",
            "--rollback-order",
            "stop-first",
        ],
    ),
}


@pytest.mark.parametrize("case", CREATE_CASES.values(), ids=CREATE_CASES)
def test_create_new_option_argv(fake: tuple[DockerClient, Path], case: Case) -> None:
    client, log = fake
    kwargs, tokens = case
    options = json.loads(json.dumps(kwargs))
    service = client.service.create("img", ["cmd"], **options)
    assert service.id == SERVICE_ID
    assert calls(log) == [["service", "create", "--quiet", *tokens, "img", "cmd"]]


def test_create_typed_values_reach_argv(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    client.service.create(
        "img",
        None,
        publish=[(80,), (8080, 80), (8080, 80, "udp")],
        stop_grace_period=timedelta(seconds=30, microseconds=1),
        restart_delay=timedelta(milliseconds=1500),
        restart_window=timedelta(),
        health_start_interval=timedelta(seconds=7),
        mode="replicated",
        replicas=2,
    )
    assert calls(log) == [
        [
            "service",
            "create",
            "--quiet",
            "--health-start-interval",
            "7s",
            "--mode",
            "replicated",
            "--publish",
            "80",
            "--publish",
            "8080:80",
            "--publish",
            "8080:80/udp",
            "--replicas",
            "2",
            "--restart-delay",
            "1500000000ns",
            "--restart-window",
            "0ns",
            "--stop-grace-period",
            "30000001000ns",
            "img",
        ]
    ]


def test_create_new_options_follow_legacy_options_in_table_order(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.create(
        "img",
        None,
        detach=True,
        labels={"k": "v"},
        mounts=[{"target": "/m"}],
        user="u",
        name="n",
        update_order="start-first",
        rollback_delay=1,
    )
    assert calls(log) == [
        [
            "service",
            "create",
            "--quiet",
            "--detach",
            "--label",
            "k=v",
            "--name",
            "n",
            "--mount",
            "target=/m",
            "--user",
            "u",
            "--update-order",
            "start-first",
            "--rollback-delay",
            "1000000000ns",
            "img",
        ]
    ]


LEGACY_CREATE_POSITIONALS = [
    "image",
    "command",
    "cap_add",
    "cap_drop",
    "constraints",
    "detach",
    "dns",
    "dns_options",
    "dns_search",
    "endpoint_mode",
    "entrypoint",
    "envs",
    "env_files",
    "generic_resources",
    "groups",
    "healthcheck",
    "health_cmd",
    "health_interval",
    "health_retries",
    "health_start_period",
    "health_timeout",
    "hosts",
    "hostname",
    "init",
    "isolation",
    "labels",
    "limit_cpu",
    "limit_memory",
    "limit_pids",
    "log_driver",
    "network",
    "restart_condition",
    "restart_max_attempts",
    "secrets",
    "mounts",
]
LEGACY_UPDATE_POSITIONALS = [
    "service",
    "detach",
    "force",
    "image",
    "with_registry_authentication",
    "quiet",
    "replicas",
]


def _positional(method: Callable[..., object]) -> list[str]:
    return [
        name
        for name, parameter in signature(method).parameters.items()
        if parameter.kind is Parameter.POSITIONAL_OR_KEYWORD and name != "self"
    ]


def _keyword_only(method: Callable[..., object]) -> set[str]:
    return {
        name
        for name, parameter in signature(method).parameters.items()
        if parameter.kind is Parameter.KEYWORD_ONLY
    }


def test_positional_parameters_are_unchanged_and_new_ones_keyword_only() -> None:
    assert _positional(ServiceCLI.create) == LEGACY_CREATE_POSITIONALS
    assert _positional(ServiceCLI.update) == LEGACY_UPDATE_POSITIONALS
    assert _positional(Service.update) == LEGACY_UPDATE_POSITIONALS[1:]
    assert list(signature(ServiceCLI.create).parameters)[-1] == "timeout"
    assert list(signature(ServiceCLI.update).parameters)[-1] == "timeout"
    assert list(signature(Service.update).parameters)[-1] == "timeout"


CREATE_PARAMETER_FOR_SLOT = {
    "constraint": "constraints",
    "container_label": "container_labels",
    "dns_option": "dns_options",
    "env": "envs",
    "env_file": "env_files",
    "generic_resource": "generic_resources",
    "group": "groups",
    "host": "hosts",
    "label": "labels",
    "log_opt": "log_opts",
    "mount": "mounts",
    "placement_pref": "placement_prefs",
    "secret": "secrets",
    "sysctl": "sysctls",
    "ulimit": "ulimits",
}


def test_every_create_slot_has_a_parameter() -> None:
    parameters = set(signature(ServiceCLI.create).parameters)
    for slot in _spec_options.CREATE_SLOTS:
        if slot.name == "no_healthcheck":
            assert "healthcheck" in parameters
            continue
        assert CREATE_PARAMETER_FOR_SLOT.get(slot.name, slot.name) in parameters


def test_every_update_slot_has_a_keyword_only_parameter() -> None:
    keyword_only = _keyword_only(ServiceCLI.update)
    positional = set(LEGACY_UPDATE_POSITIONALS)
    for slot in _spec_options.UPDATE_SLOTS:
        name = "log_opts" if slot.name == "log_opt" else slot.name
        assert name in keyword_only or name in positional


def test_service_handle_update_accepts_every_cli_option() -> None:
    cli = set(signature(ServiceCLI.update).parameters) - {"self", "service"}
    handle = set(signature(Service.update).parameters) - {"self"}
    assert handle == cli


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"mode": "bad"}, ValueError),
        ({"mode": 1}, ValueError),
        ({"restart_delay": "5s"}, TypeError),
        ({"restart_delay": True}, TypeError),
        ({"restart_window": -1}, ValueError),
        ({"stop_grace_period": 2**63}, ValueError),
        ({"health_start_interval": "2"}, TypeError),
        ({"publish": ["80"]}, TypeError),
        ({"publish": [80]}, TypeError),
        ({"sysctls": ["a=b"]}, TypeError),
        ({"log_opts": "a=b"}, TypeError),
        ({"container_labels": ["a"]}, TypeError),
        ({"config": ["name"]}, TypeError),
        ({"mounts": ["type=volume"]}, TypeError),
        ({"envs": ["A=1"]}, TypeError),
    ],
)
def test_invalid_create_options_precede_executable_lookup(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    client = DockerClient(client_call=MISSING)
    options = json.loads(json.dumps(kwargs))
    with pytest.raises(error):
        client.service.create("secret-image", None, **options)


def test_oversized_port_tuples_precede_executable_lookup() -> None:
    client = DockerClient(client_call=MISSING)
    oversized = tuple(json.loads('[1, 2, "tcp", 4]'))
    with pytest.raises(ValueError):
        client.service.create("img", None, publish=[oversized])
    with pytest.raises(ValueError):
        client.service.update("svc", publish_add=[oversized])
    with pytest.raises(ValueError):
        client.service.update("svc", publish_rm=[oversized])


def test_create_with_valid_options_does_reach_executable_lookup() -> None:
    client = DockerClient(client_call=MISSING)
    with pytest.raises(ClientNotFoundError):
        client.service.create("img", None, name="web")


def test_create_validation_order_is_timeout_policy_then_options() -> None:
    client = DockerClient(client_call=MISSING)
    invalid = json.loads('{"mode": "bad"}')
    with pytest.raises(ValueError) as timeout_failure:
        client.service.create(
            "img", None, timeout=0.0, update_parallelism=True, **invalid
        )
    assert "mode" not in str(timeout_failure.value)
    with pytest.raises(TypeError):
        client.service.create("img", None, update_parallelism=True, **invalid)


def test_create_failure_before_launch_leaves_no_calls(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    invalid = json.loads('{"mode": "bad"}')
    with pytest.raises(ValueError):
        client.service.create("img", None, **invalid)
    assert calls(log) == []


UPDATE_CASES: dict[str, Case] = {
    "args": ({"args": ["sleep", "10"]}, ["--args", "sleep 10"]),
    "args-quoted": (
        {"args": ["echo", "a b", "it's"]},
        ["--args", "echo 'a b' 'it'\"'\"'s'"],
    ),
    "args-clear": ({"args": []}, ["--args", ""]),
    "cap-add": (
        {"cap_add": ["NET_ADMIN", "SYS_TIME"]},
        ["--cap-add", "NET_ADMIN", "--cap-add", "SYS_TIME"],
    ),
    "cap-drop": ({"cap_drop": ["MKNOD"]}, ["--cap-drop", "MKNOD"]),
    "config-add": (
        {"config_add": [{"source": "c", "target": "/c", "mode": "0440"}]},
        ["--config-add", "source=c,target=/c,mode=0440"],
    ),
    "config-rm": ({"config_rm": ["c", "d"]}, ["--config-rm", "c", "--config-rm", "d"]),
    "constraint-add": (
        {"constraint_add": ["node.role==worker"]},
        ["--constraint-add", "node.role==worker"],
    ),
    "constraint-rm": (
        {"constraint_rm": ["node.role==worker"]},
        ["--constraint-rm", "node.role==worker"],
    ),
    "container-label-add": (
        {"container_label_add": {"a": "b"}},
        ["--container-label-add", "a=b"],
    ),
    "container-label-rm": (
        {"container_label_rm": ["a"]},
        ["--container-label-rm", "a"],
    ),
    "credential-spec": (
        {"credential_spec": "file://spec.json"},
        ["--credential-spec", "file://spec.json"],
    ),
    "dns-add": ({"dns_add": ["1.1.1.1"]}, ["--dns-add", "1.1.1.1"]),
    "dns-rm": ({"dns_rm": ["1.1.1.1"]}, ["--dns-rm", "1.1.1.1"]),
    "dns-option-add": (
        {"dns_option_add": ["ndots:2"]},
        ["--dns-option-add", "ndots:2"],
    ),
    "dns-option-rm": ({"dns_option_rm": ["ndots:2"]}, ["--dns-option-rm", "ndots:2"]),
    "dns-search-add": (
        {"dns_search_add": ["example.com"]},
        ["--dns-search-add", "example.com"],
    ),
    "dns-search-rm": (
        {"dns_search_rm": ["example.com"]},
        ["--dns-search-rm", "example.com"],
    ),
    "endpoint-mode": ({"endpoint_mode": "dnsrr"}, ["--endpoint-mode", "dnsrr"]),
    "entrypoint": ({"entrypoint": "/entry"}, ["--entrypoint", "/entry"]),
    "env-add": (
        {"env_add": {"E1_APP_TOKEN": "secret value"}},
        ["--env-add", "E1_APP_TOKEN"],
    ),
    "env-rm": ({"env_rm": ["A", "B"]}, ["--env-rm", "A", "--env-rm", "B"]),
    "generic-resource-add": (
        {"generic_resource_add": ["gpu=2"]},
        ["--generic-resource-add", "gpu=2"],
    ),
    "generic-resource-rm": (
        {"generic_resource_rm": ["gpu"]},
        ["--generic-resource-rm", "gpu"],
    ),
    "group-add": ({"group_add": ["wheel"]}, ["--group-add", "wheel"]),
    "group-rm": ({"group_rm": ["wheel"]}, ["--group-rm", "wheel"]),
    "health-cmd": ({"health_cmd": "echo ok"}, ["--health-cmd", "echo ok"]),
    "health-interval": ({"health_interval": 5}, ["--health-interval", "5s"]),
    "health-retries-zero": ({"health_retries": 0}, ["--health-retries", "0"]),
    "health-start-interval": (
        {"health_start_interval": 2},
        ["--health-start-interval", "2s"],
    ),
    "health-start-period": (
        {"health_start_period": 7},
        ["--health-start-period", "7s"],
    ),
    "health-timeout": ({"health_timeout": 9}, ["--health-timeout", "9s"]),
    "host-add": ({"host_add": {"db": "10.0.0.5"}}, ["--host-add", "db:10.0.0.5"]),
    "host-rm": ({"host_rm": {"db": "10.0.0.5"}}, ["--host-rm", "db:10.0.0.5"]),
    "hostname": ({"hostname": "box"}, ["--hostname", "box"]),
    "init-on": ({"init": True}, ["--init"]),
    "init-off": ({"init": False}, ["--init=false"]),
    "isolation": ({"isolation": "process"}, ["--isolation", "process"]),
    "label-add": ({"label_add": {"k": "v"}}, ["--label-add", "k=v"]),
    "label-rm": ({"label_rm": ["k"]}, ["--label-rm", "k"]),
    "limit-cpu": ({"limit_cpu": 0.5}, ["--limit-cpu", "0.5"]),
    "limit-memory": ({"limit_memory": "1g"}, ["--limit-memory", "1g"]),
    "limit-pids-zero": ({"limit_pids": 0}, ["--limit-pids", "0"]),
    "log-driver": ({"log_driver": "json-file"}, ["--log-driver", "json-file"]),
    "log-opt": ({"log_opts": {"max-size": "1m"}}, ["--log-opt", "max-size=1m"]),
    "max-concurrent": ({"max_concurrent": 3}, ["--max-concurrent", "3"]),
    "memory-swap-forwarded-verbatim": ({"memory_swap": -1}, ["--memory-swap", "-1"]),
    "memory-swappiness-zero": ({"memory_swappiness": 0}, ["--memory-swappiness", "0"]),
    "mount-add": (
        {"mount_add": [{"type": "volume", "source": "v", "target": "/v"}]},
        ["--mount-add", "type=volume,source=v,target=/v"],
    ),
    "mount-rm": ({"mount_rm": ["/v"]}, ["--mount-rm", "/v"]),
    "network-add": (
        {"network_add": ["name=net,alias=web"]},
        ["--network-add", "name=net,alias=web"],
    ),
    "network-rm": ({"network_rm": ["net"]}, ["--network-rm", "net"]),
    "no-healthcheck": ({"no_healthcheck": True}, ["--no-healthcheck"]),
    "no-resolve-image": ({"no_resolve_image": True}, ["--no-resolve-image"]),
    "oom-score-adj": ({"oom_score_adj": -500}, ["--oom-score-adj", "-500"]),
    "placement-pref-add": (
        {"placement_pref_add": ["spread=node.labels.zone"]},
        ["--placement-pref-add", "spread=node.labels.zone"],
    ),
    "placement-pref-rm": (
        {"placement_pref_rm": ["spread=node.labels.zone"]},
        ["--placement-pref-rm", "spread=node.labels.zone"],
    ),
    "publish-add": (
        {"publish_add": [{"published": 8080, "target": 80}]},
        ["--publish-add", "published=8080,target=80"],
    ),
    "publish-rm": (
        {"publish_rm": [{"target": 80, "protocol": "tcp"}]},
        ["--publish-rm", "target=80,protocol=tcp"],
    ),
    "read-only-on": ({"read_only": True}, ["--read-only"]),
    "read-only-off": ({"read_only": False}, ["--read-only=false"]),
    "replicas-max-per-node": (
        {"replicas_max_per_node": 2},
        ["--replicas-max-per-node", "2"],
    ),
    "reserve-cpu": ({"reserve_cpu": 0.25}, ["--reserve-cpu", "0.25"]),
    "reserve-memory": ({"reserve_memory": "64m"}, ["--reserve-memory", "64m"]),
    "restart-condition": (
        {"restart_condition": "on-failure"},
        ["--restart-condition", "on-failure"],
    ),
    "restart-delay": ({"restart_delay": 5}, ["--restart-delay", "5000000000ns"]),
    "restart-max-attempts-zero": (
        {"restart_max_attempts": 0},
        ["--restart-max-attempts", "0"],
    ),
    "restart-window": ({"restart_window": 60}, ["--restart-window", "60000000000ns"]),
    "rollback": ({"rollback": True}, ["--rollback"]),
    "secret-add": (
        {"secret_add": [{"source": "s", "target": "t"}]},
        ["--secret-add", "source=s,target=t"],
    ),
    "secret-rm": ({"secret_rm": ["s"]}, ["--secret-rm", "s"]),
    "stop-grace-period": (
        {"stop_grace_period": 3},
        ["--stop-grace-period", "3000000000ns"],
    ),
    "stop-signal": ({"stop_signal": "SIGUSR1"}, ["--stop-signal", "SIGUSR1"]),
    "sysctl-add": (
        {"sysctl_add": {"net.core.somaxconn": "1024"}},
        ["--sysctl-add", "net.core.somaxconn=1024"],
    ),
    "sysctl-rm": (
        {"sysctl_rm": ["net.core.somaxconn"]},
        ["--sysctl-rm", "net.core.somaxconn"],
    ),
    "tty-on": ({"tty": True}, ["--tty"]),
    "tty-off": ({"tty": False}, ["--tty=false"]),
    "ulimit-add": (
        {"ulimit_add": ["nofile=1024:2048"]},
        ["--ulimit-add", "nofile=1024:2048"],
    ),
    "ulimit-rm": ({"ulimit_rm": ["nofile"]}, ["--ulimit-rm", "nofile"]),
    "user": ({"user": "1000:1000"}, ["--user", "1000:1000"]),
    "workdir": ({"workdir": "/srv"}, ["--workdir", "/srv"]),
}


@pytest.mark.parametrize("case", UPDATE_CASES.values(), ids=UPDATE_CASES)
def test_update_option_argv_and_handle_forwarding(
    fake: tuple[DockerClient, Path], case: Case
) -> None:
    client, log = fake
    kwargs, tokens = case
    options = json.loads(json.dumps(kwargs))
    handle = Service(client.client_config, "identity", is_immutable_id=True)
    assert client.service.update("identity", **options) is None
    assert handle.update(**options) is None
    expected = ["service", "update", *tokens, "identity"]
    assert calls(log) == [expected, expected]


def test_update_omitted_tristate_options_emit_nothing(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.update("svc", init=None, tty=None, read_only=None, args=None)
    assert calls(log) == [["service", "update", "svc"]]


def test_update_typed_values_reach_argv(fake: tuple[DockerClient, Path]) -> None:
    client, log = fake
    client.service.update(
        "svc",
        publish_add=[(80,), (8080, 80), (8080, 80, "udp")],
        publish_rm=[(443,)],
        stop_grace_period=timedelta(seconds=30, microseconds=1),
        restart_delay=timedelta(milliseconds=1500),
        health_interval=timedelta(seconds=3),
    )
    assert calls(log) == [
        [
            "service",
            "update",
            "--health-interval",
            "3s",
            "--publish-add",
            "80",
            "--publish-add",
            "8080:80",
            "--publish-add",
            "8080:80/udp",
            "--publish-rm",
            "443",
            "--restart-delay",
            "1500000000ns",
            "--stop-grace-period",
            "30000001000ns",
            "svc",
        ]
    ]


def test_update_legacy_flags_precede_options_and_policies(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.update(
        "svc",
        True,
        True,
        "img",
        True,
        True,
        3,
        env_rm=["A"],
        label_add={"k": "v"},
        update_delay=1,
    )
    assert calls(log) == [
        [
            "service",
            "update",
            "--force",
            "--image",
            "img",
            "--with-registry-auth",
            "--detach",
            "--quiet",
            "--env-rm",
            "A",
            "--label-add",
            "k=v",
            "--replicas",
            "3",
            "--update-delay",
            "1000000000ns",
            "svc",
        ]
    ]


def test_update_env_values_travel_in_the_child_environment(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    client.service.update("svc", env_add={"E1_APP_TOKEN": "secret=value"})
    assert "secret=value" not in " ".join(calls(log)[0])
    assert records(log)[0]["env"] == {"E1_APP_TOKEN": "secret=value"}


def test_update_strict_env_transport_rejects_before_launch(tmp_path: Path) -> None:
    client = DockerClient(client_call=MISSING, strict_env_transport=True)
    with pytest.raises(ValueError) as rejected:
        client.service.update("svc", env_add={"DOCKER_HOST": "secret-value"})
    assert "secret-value" not in str(rejected.value)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"restart_delay": "5s"}, TypeError),
        ({"stop_grace_period": -1}, ValueError),
        ({"health_interval": "5"}, TypeError),
        ({"publish_rm": ["80"]}, TypeError),
        ({"args": "sleep 10"}, TypeError),
        ({"env_add": ["A=1"]}, TypeError),
        ({"label_add": ["a"]}, TypeError),
        ({"host_add": ["a:b"]}, TypeError),
        ({"mount_add": ["type=volume"]}, TypeError),
        ({"secret_add": ["name"]}, TypeError),
        ({"config_add": [1]}, TypeError),
        ({"sysctl_add": ["a=b"]}, TypeError),
        ({"log_opts": "a=b"}, TypeError),
    ],
)
def test_invalid_update_options_precede_executable_lookup(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    client = DockerClient(client_call=MISSING)
    options = json.loads(json.dumps(kwargs))
    with pytest.raises(error):
        client.service.update("secret-service", **options)
    handle = Service(client.client_config, "secret-service", is_immutable_id=True)
    with pytest.raises(error):
        handle.update(**options)


def test_update_with_valid_options_does_reach_executable_lookup() -> None:
    client = DockerClient(client_call=MISSING)
    with pytest.raises(ClientNotFoundError):
        client.service.update("svc", cap_add=["NET_ADMIN"])


def test_update_validation_order_is_timeout_policy_then_options() -> None:
    client = DockerClient(client_call=MISSING)
    oversized = tuple(json.loads("[1, 2, 3, 4]"))
    with pytest.raises(ValueError) as timeout_failure:
        client.service.update(
            "svc", timeout=0.0, update_parallelism=True, publish_add=[oversized]
        )
    assert "port" not in str(timeout_failure.value)
    with pytest.raises(TypeError):
        client.service.update("svc", update_parallelism=True, publish_add=[oversized])


def test_failed_update_still_releases_the_handle_pin(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("update",), stderr="boom\n", exit_code=1)]
    )
    handle = Service(client.client_config, SERVICE_ID, is_immutable_id=True)
    handle._set_pinned_inspect_result(
        ServiceInspectResult.model_validate({"ID": SERVICE_ID})
    )
    assert handle._pinned
    with pytest.raises(DockerException):
        handle.update(label_add={"k": "v"})
    assert not handle._pinned
    assert calls(log) == [["service", "update", "--label-add", "k=v", SERVICE_ID]]


def test_inspect_pretty_returns_text_and_never_parses_or_caches(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    assert client.service.inspect("web", pretty=True) == "ID: x\nName: web"
    assert client.service.inspect(["web", "db"], pretty=True) == "ID: x\nName: web"
    assert calls(log) == [
        ["service", "inspect", "--pretty", "web"],
        ["service", "inspect", "--pretty", "web", "db"],
    ]


def test_inspect_pretty_empty_selection_does_not_launch(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    assert client.service.inspect([], pretty=True) == ""
    assert client.service.inspect([]) == []
    assert calls(log) == []


def test_inspect_without_pretty_keeps_json_argv(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    service = client.service.inspect("web", pretty=False)
    assert service.id == SERVICE_ID
    assert calls(log) == [["service", "inspect", "web"]]


def test_ps_filters_are_forwarded_in_order_before_services(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    tasks = client.service.ps(
        ["web", "db"],
        filters=[("desired-state", "running"), ("node", "n1"), ("node", "n2")],
    )
    assert [task.id for task in tasks] == ["task-one", "task-two"]
    assert calls(log) == [
        [
            "service",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "desired-state=running",
            "--filter",
            "node=n1",
            "--filter",
            "node=n2",
            "web",
            "db",
        ]
    ]


def test_ps_filters_accept_generators_and_the_handle_forwards_them(
    fake: tuple[DockerClient, Path],
) -> None:
    client, log = fake
    handle = Service(client.client_config, SERVICE_ID, is_immutable_id=True)
    handle.ps(filters=(pair for pair in [("name", "web.1")]))
    client.service.ps("web", filters=None)
    client.service.ps("web", filters=())
    assert calls(log) == [
        [
            "service",
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "name=web.1",
            SERVICE_ID,
        ],
        ["service", "ps", "--quiet", "--no-trunc", "web"],
        ["service", "ps", "--quiet", "--no-trunc", "web"],
    ]


@pytest.mark.parametrize("selection", ["web", []])
def test_ps_rejects_mapping_filters_before_lookup(selection: str | list[str]) -> None:
    client = DockerClient(client_call=MISSING)
    mapping = json.loads('{"desired-state": "running"}')
    with pytest.raises(TypeError):
        client.service.ps(selection, filters=mapping)
