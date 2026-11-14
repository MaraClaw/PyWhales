"""Buildx core options for the Docker CLI 29 plugin set through a fake CLI.

Every case runs a recording Python script as the Docker CLI, so no daemon is used.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from pywhales import DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.buildx.cli_wrapper import Builder
from pywhales.components.buildx.dap import BuildxDapCLI
from pywhales.components.buildx.history import BuildxHistoryCLI
from pywhales.components.buildx.models import BuildxDiskUsage
from pywhales.components.buildx.policy import BuildxPolicyCLI
from pywhales.components.buildx.replay import BuildxReplayCLI
from pywhales.exceptions import DockerTimeout
from pywhales.test_utils import get_all_jsons
from tests.pywhales.components.argv_fakes import (
    Rule,
    calls,
    fake_cli_call,
    make_fake_cli,
    records,
)

_BUILDER_LINE = json.dumps({"Name": "selected", "Driver": "docker"})


def _create_rules(name: str) -> list[Rule]:
    """Answer `create` with a builder name and the listing that resolves it."""
    return [
        Rule(match=("create",), stdout=f"{name}\n"),
        Rule(match=("ls",), stdout=json.dumps({"Name": name, "Driver": "docker"})),
    ]


def test_build_argv_snapshot_with_rich_arguments(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.build(
        "ctx",
        add_hosts={"a.example": "192.0.2.1", "b.example": "192.0.2.2"},
        allow=["network.host", "security.insecure"],
        attest={"type": "sbom", "generator": "image"},
        build_args={"VERSION": "3", "NAME": "x"},
        build_contexts={"deps": "../deps", "base": "docker-image://alpine"},
        builder="selected",
        cache=False,
        cache_from=[
            {"type": "local", "src": "in"},
            {"type": "registry", "ref": "r/app:cache"},
        ],
        cache_to={"type": "local", "dest": "out", "mode": "max"},
        file=Path("Containerfile"),
        labels={"k": "v", "l": "w"},
        metadata_file="meta.json",
        network="none",
        output={"type": "oci", "dest": "o.tar"},
        platforms=["linux/amd64", "linux/arm64"],
        progress="plain",
        provenance={"mode": "max"},
        pull=True,
        push=False,
        sbom=True,
        secrets=["id=a,src=./a", "id=b"],
        ssh="default",
        tags=["app:1", "app:2"],
        target="final",
        call="build",
        no_cache_filter=["deps", "build"],
        annotations=["a=1", "b=2"],
    )
    assert calls(log) == [
        [
            "buildx",
            "build",
            "--progress",
            "plain",
            "--add-host",
            "a.example:192.0.2.1",
            "--add-host",
            "b.example:192.0.2.2",
            "--allow",
            "network.host",
            "--allow",
            "security.insecure",
            "--attest",
            "type=sbom,generator=image",
            "--build-arg",
            "VERSION",
            "--build-arg",
            "NAME",
            "--build-context",
            "deps=../deps",
            "--build-context",
            "base=docker-image://alpine",
            "--builder",
            "selected",
            "--label",
            "k=v",
            "--label",
            "l=w",
            "--ssh",
            "default",
            "--provenance",
            "mode=max",
            "--pull",
            "--sbom=true",
            "--metadata-file",
            "meta.json",
            "--file",
            "Containerfile",
            "--target",
            "final",
            "--cache-from",
            "type=local,src=in",
            "--cache-from",
            "type=registry,ref=r/app:cache",
            "--cache-to",
            "type=local,dest=out,mode=max",
            "--secret",
            "id=a,src=./a",
            "--secret",
            "id=b",
            "--output",
            "type=oci,dest=o.tar",
            "--platform",
            "linux/amd64,linux/arm64",
            "--network",
            "none",
            "--no-cache",
            "--tag",
            "app:1",
            "--tag",
            "app:2",
            "--call",
            "build",
            "--no-cache-filter",
            "deps",
            "--no-cache-filter",
            "build",
            "--annotation",
            "a=1",
            "--annotation",
            "b=2",
            "ctx",
        ]
    ]


def test_build_argv_snapshot_with_scalar_value_forms(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.build(
        ".",
        cache_from={"type": "gha"},
        cache_to="user/app:cache",
        load=True,
        progress=False,
        provenance=True,
        sbom={"generator": "image"},
        secrets="id=one",
        tags="solo",
        call="check",
        no_cache_filter="deps",
    )
    assert calls(log) == [
        [
            "buildx",
            "build",
            "--provenance=true",
            "--sbom",
            "generator=image",
            "--load",
            "--cache-from",
            "type=gha",
            "--cache-to",
            "user/app:cache",
            "--secret",
            "id=one",
            "--tag",
            "solo",
            "--call",
            "check",
            "--no-cache-filter",
            "deps",
            ".",
        ]
    ]


def test_build_forwards_new_options_after_existing_ones(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.build(
        ".",
        push=True,
        no_cache_filter="deps",
        cgroup_parent="/pyw",
        policies=["filename=a.rego,strict=true", "disabled=true"],
        quiet=True,
        resources={"memory": "2g", "cpu-quota": 50000},
        shm_size="64m",
        ulimits=["nofile=1024:2048", "nproc=10"],
        debug=True,
    )
    assert calls(log) == [
        [
            "buildx",
            "build",
            "--debug",
            "--push",
            "--no-cache-filter",
            "deps",
            "--cgroup-parent",
            "/pyw",
            "--policy",
            "filename=a.rego,strict=true",
            "--policy",
            "disabled=true",
            "--quiet",
            "--resource",
            "memory=2g",
            "--resource",
            "cpu-quota=50000",
            "--shm-size",
            "64m",
            "--ulimit",
            "nofile=1024:2048",
            "--ulimit",
            "nproc=10",
            ".",
        ]
    ]


def test_build_accepts_single_values_and_integer_shm_size(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.build(
        ".", push=True, policies="strict=true", ulimits="nofile=1:2", shm_size=0
    )
    assert calls(log) == [
        [
            "buildx",
            "build",
            "--push",
            "--policy",
            "strict=true",
            "--shm-size",
            "0",
            "--ulimit",
            "nofile=1:2",
            ".",
        ]
    ]


def test_build_quiet_with_streaming_is_rejected_before_any_launch(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.build(".", quiet=True, stream_logs=True, progress="plain")
    assert calls(log) == []


def test_build_validates_timeout_before_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.build(".", quiet=True, timeout=0)
    assert calls(log) == []


def test_dap_build_uses_the_shared_assembler_with_inherited_stdio(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("dap",), env=("VERSION",))])
    result = client.buildx.dap.build(
        "ctx",
        build_args={"VERSION": "3"},
        builder="selected",
        cgroup_parent="/pyw",
        iidfile=tmp_path / "id",
        policies="strict=true",
        quiet=True,
        resources={"memory": "2g"},
        shm_size="64m",
        tags=["app:1"],
        ulimits="nofile=1:2",
        call="check",
        debug=True,
    )
    assert result is None
    assert calls(log) == [
        [
            "buildx",
            "dap",
            "build",
            "--debug",
            "--build-arg",
            "VERSION",
            "--builder",
            "selected",
            "--tag",
            "app:1",
            "--call",
            "check",
            "--cgroup-parent",
            "/pyw",
            "--policy",
            "strict=true",
            "--quiet",
            "--resource",
            "memory=2g",
            "--shm-size",
            "64m",
            "--ulimit",
            "nofile=1:2",
            "--iidfile",
            str(tmp_path / "id"),
            "ctx",
        ]
    ]
    assert records(log)[0]["env"] == {"VERSION": "3"}


def test_dap_build_validates_before_lookup(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.dap.build(".", timeout=-1)
    assert calls(log) == []


def test_dap_build_deadline_raises_docker_timeout(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("dap",), sleep=30)])
    with pytest.raises(DockerTimeout):
        client.buildx.dap.build(".", timeout=0.5)
    assert len(calls(log)) == 1


def test_sub_clis_are_wired_with_the_shared_config(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path)
    buildx = client.buildx
    assert isinstance(buildx.dap, BuildxDapCLI)
    assert isinstance(buildx.history, BuildxHistoryCLI)
    assert isinstance(buildx.policy, BuildxPolicyCLI)
    assert isinstance(buildx.replay, BuildxReplayCLI)
    for sub_cli in (buildx.dap, buildx.history, buildx.policy, buildx.replay):
        assert sub_cli.client_config is client.client_config


def test_dial_stdio_argv_and_inherited_stdio(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    result = client.buildx.dial_stdio(
        builder="selected", platform="linux/arm64", progress="rawjson", debug=True
    )
    assert result is None
    assert calls(log) == [
        [
            "buildx",
            "dial-stdio",
            "--debug",
            "--builder",
            "selected",
            "--platform",
            "linux/arm64",
            "--progress",
            "rawjson",
        ]
    ]


def test_dial_stdio_defaults_add_no_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.dial_stdio()
    assert calls(log) == [["buildx", "dial-stdio"]]


def test_dial_stdio_validates_before_lookup(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.dial_stdio(timeout=0)
    assert calls(log) == []


def test_dial_stdio_deadline_raises_docker_timeout(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("dial-stdio",), sleep=30)])
    with pytest.raises(DockerTimeout):
        client.buildx.dial_stdio(timeout=0.5)


def test_bake_forwards_new_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("--print",), stdout='{"target": {}}')]
    )
    client.buildx.bake(
        "app",
        allow=["network.host", "fs=/tmp"],
        policies=["strict=true", "log-level=warn"],
        provenance={"mode": "max"},
        sbom=False,
        variables_set={"TAG": "v1", "NAME": "x"},
        debug=True,
        return_config=False,
    )
    assert calls(log) == [
        [
            "buildx",
            "bake",
            "--debug",
            "--allow",
            "network.host",
            "--allow",
            "fs=/tmp",
            "--policy",
            "strict=true",
            "--policy",
            "log-level=warn",
            "--provenance",
            "mode=max",
            "--sbom=false",
            "--var",
            "TAG=v1",
            "--var",
            "NAME=x",
            "app",
        ]
    ]


def test_bake_boolean_attestations_use_equals_form(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.bake(provenance=True, sbom=True, return_config=False)
    assert calls(log) == [["buildx", "bake", "--provenance=true", "--sbom=true"]]


def test_bake_list_is_one_launch_returning_the_table(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("--list",), stdout="TARGET\tDESCRIPTION\napp\t\n")]
    )
    assert client.buildx.bake("app", list="targets") == "TARGET\tDESCRIPTION\napp\t"
    assert client.buildx.bake("app", list="variables") == "TARGET\tDESCRIPTION\napp\t"
    assert calls(log) == [
        ["buildx", "bake", "--list", "targets", "app"],
        ["buildx", "bake", "--list", "variables", "app"],
    ]


def test_bake_list_ignores_return_config_and_streams(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("--list",), stdout="out\n")])
    assert client.buildx.bake(list="targets", return_config=True) == "out"
    with client.buildx.bake(list="variables", stream_logs=True) as stream:
        assert "".join(stream) == "out\n"
    assert [argv[2:4] for argv in calls(log)] == [
        ["--list", "targets"],
        ["--list", "variables"],
    ]


def test_bake_list_conflicts_with_print_before_any_launch(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.bake(print=True, list="targets")
    assert calls(log) == []


def test_create_emits_the_canonical_buildkitd_config_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _create_rules("made"))
    builder = client.buildx.create(
        "ctx",
        bootstrap=True,
        buildkitd_flags="--debug",
        config=tmp_path / "buildkitd.toml",
        driver="docker-container",
        name="made",
    )
    assert isinstance(builder, Builder)
    assert builder.name == "made"
    assert calls(log)[0] == [
        "buildx",
        "create",
        "--bootstrap",
        "--buildkitd-flags",
        "--debug",
        "--buildkitd-config",
        str(tmp_path / "buildkitd.toml"),
        "--driver",
        "docker-container",
        "--name",
        "made",
        "ctx",
    ]
    assert "--config" not in calls(log)[0]


@pytest.mark.parametrize(
    ("status_timeout", "text"),
    [(5, "5s"), (timedelta(seconds=20), "20s"), (timedelta(milliseconds=2500), "2s")],
)
def test_create_node_and_status_timeout(
    tmp_path: Path, status_timeout: int | timedelta, text: str
) -> None:
    client, log = make_fake_cli(tmp_path, _create_rules("made"))
    builder = client.buildx.create(
        name="made", node="n2", append=True, status_timeout=status_timeout, debug=True
    )
    assert builder.name == "made"
    assert calls(log)[0] == [
        "buildx",
        "create",
        "--debug",
        "--append",
        "--name",
        "made",
        "--node",
        "n2",
        "--timeout",
        text,
    ]


def test_create_leave_returns_none_and_never_looks_a_builder_up(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    assert client.buildx.create(name="made", node="n2", leave=True) is None
    assert calls(log) == [
        ["buildx", "create", "--name", "made", "--leave", "--node", "n2"]
    ]


def test_list_forwards_status_timeout_and_keeps_json_format(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("ls",), stdout=_BUILDER_LINE)])
    builders = client.buildx.list(status_timeout=7)
    assert [builder.name for builder in builders] == ["selected"]
    assert calls(log) == [
        ["buildx", "ls", "--format", "{{json . }}", "--timeout", "7s"]
    ]


def test_list_debug_returns_table_then_stderr_without_json_format(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("ls",), stdout="NAME/NODE  DRIVER\n", stderr="debug log\n")],
    )
    result = client.buildx.list(debug=True, status_timeout=3)
    assert result == "NAME/NODE  DRIVER\ndebug log"
    assert calls(log) == [["buildx", "ls", "--debug", "--timeout", "3s"]]


def test_inspect_forwards_status_timeout_to_both_launches(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [
            Rule(match=("inspect",), stdout="Name: selected\n"),
            Rule(match=("ls",), stdout=_BUILDER_LINE),
        ],
    )
    builder = client.buildx.inspect(
        "selected", True, status_timeout=timedelta(seconds=9)
    )
    assert isinstance(builder, Builder)
    assert calls(log) == [
        ["buildx", "inspect", "selected", "--bootstrap", "--timeout", "9s"],
        ["buildx", "ls", "--format", "{{json . }}", "--timeout", "9s"],
    ]


def test_inspect_debug_returns_text_without_a_listing(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("inspect",), stdout="Name: selected\n", stderr="debug log\n")],
    )
    result = client.buildx.inspect("selected", debug=True)
    assert result == "Name: selected\ndebug log"
    assert calls(log) == [["buildx", "inspect", "--debug", "selected"]]


def test_prune_forwards_new_options_without_local_deadline_flag(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.prune(
        True,
        {"until": "24h"},
        max_used_space="10GB",
        min_free_space=2048,
        reserved_space="1GB",
        verbose=True,
        status_timeout=timedelta(seconds=30),
        debug=True,
        timeout=60,
    )
    assert calls(log) == [
        [
            "buildx",
            "prune",
            "--debug",
            "--force",
            "--all",
            "--filter",
            "until=24h",
            "--max-used-space",
            "10GB",
            "--min-free-space",
            "2048",
            "--reserved-space",
            "1GB",
            "--verbose",
            "--timeout",
            "30s",
        ]
    ]


def test_prune_defaults_keep_the_legacy_argv(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.prune(timeout=5)
    assert calls(log) == [["buildx", "prune", "--force"]]


def test_prune_streams_new_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("prune",), stdout="done\n")])
    with client.buildx.prune(
        stream_logs=True, reserved_space=1, verbose=True, status_timeout=4
    ) as stream:
        assert "".join(stream) == "done\n"
    assert calls(log) == [
        [
            "buildx",
            "prune",
            "--force",
            "--reserved-space",
            "1",
            "--verbose",
            "--timeout",
            "4s",
        ]
    ]


def test_remove_argv_for_a_named_the_current_and_inactive_builders(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    client.buildx.remove()
    client.buildx.remove("old")
    client.buildx.remove(
        "old",
        force=True,
        keep_daemon=True,
        keep_state=True,
        status_timeout=8,
        debug=True,
    )
    client.buildx.remove(all_inactive=True, force=True)
    assert calls(log) == [
        ["buildx", "rm"],
        ["buildx", "rm", "old"],
        [
            "buildx",
            "rm",
            "--debug",
            "--force",
            "--keep-daemon",
            "--keep-state",
            "--timeout",
            "8s",
            "old",
        ],
        ["buildx", "rm", "--all-inactive", "--force"],
    ]


def test_remove_rejects_all_inactive_with_a_builder_before_any_launch(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    with pytest.raises(ValueError):
        client.buildx.remove("old", all_inactive=True)
    assert calls(log) == []


def test_builder_handle_remove_forwards_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path)
    Builder(client.client_config, "old", is_immutable_id=True).remove(
        force=True, keep_state=True, status_timeout=2, debug=True
    )
    assert calls(log) == [
        [
            "buildx",
            "rm",
            "--debug",
            "--force",
            "--keep-state",
            "--timeout",
            "2s",
            "old",
        ]
    ]


def test_builder_context_exit_still_removes_without_options(tmp_path: Path) -> None:
    client, log = make_fake_cli(tmp_path, _create_rules("tmp"))
    with client.buildx.create(name="tmp"):
        pass
    assert calls(log)[-1] == ["buildx", "rm", "tmp"]


def test_stop_use_and_version_forward_debug(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("version",), stdout="v0.39.0\n")]
    )
    client.buildx.stop("b", debug=True)
    client.buildx.stop(None, debug=True)
    client.buildx.use("b", default=True, global_=True, debug=True)
    assert client.buildx.version(debug=True) == "v0.39.0"
    assert calls(log) == [
        ["buildx", "stop", "--debug", "b"],
        ["buildx", "stop", "--debug"],
        ["buildx", "use", "--debug", "--default", "--global", "b"],
        ["buildx", "version", "--debug"],
    ]


def _usage_lines() -> str:
    return "".join(
        json.dumps(json.loads(path.read_text())) + "\n"
        for path in get_all_jsons("buildx_du")
    )


def test_disk_usage_parses_json_records_from_the_real_cli_shape(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path, [Rule(match=("du",), stdout=_usage_lines())])
    usage = client.buildx.disk_usage()
    assert calls(log) == [["buildx", "du", "--format", "json"]]
    assert all(isinstance(record, BuildxDiskUsage) for record in usage)
    assert [record.type for record in usage] == [
        "regular",
        "source.local",
        "frontend",
        "exec.cachemount",
    ]
    first, local, _, cache = usage
    assert first.id == "atkzke2v20olpo5qijm2b3icu"
    assert first.parents == ["81otmxnocubngk1equ4vi6f7n"]
    assert first.size == "4.128kB"
    assert first.last_used_at == "34 hours ago"
    assert first.created_at == "2026-11-13 01:36:32.85912105 +0000 UTC"
    assert (first.mutable, first.reclaimable, first.shared) == (False, True, True)
    assert first.usage_count == 1
    assert local.parents is None
    assert local.mutable is True
    assert cache.reclaimable is False


def test_disk_usage_forwards_filters_builder_and_status_timeout(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    assert (
        client.buildx.disk_usage(
            filters=[("until", "24h"), ("type", "regular")],
            builder="selected",
            status_timeout=timedelta(seconds=11),
            timeout=30,
        )
        == []
    )
    assert calls(log) == [
        [
            "buildx",
            "du",
            "--format",
            "json",
            "--builder",
            "selected",
            "--filter",
            "until=24h",
            "--filter",
            "type=regular",
            "--timeout",
            "11s",
        ]
    ]


def test_disk_usage_verbose_returns_the_report_text(tmp_path: Path) -> None:
    client, log = make_fake_cli(
        tmp_path,
        [Rule(match=("du",), stdout="ID:           abc\nType:         regular\n")],
    )
    assert (
        client.buildx.disk_usage(verbose=True)
        == "ID:           abc\nType:         regular"
    )
    assert calls(log) == [["buildx", "du", "--verbose"]]


@pytest.mark.parametrize(
    ("verbose", "expected"),
    [
        (False, ["buildx", "du", "--debug"]),
        (True, ["buildx", "du", "--debug", "--verbose"]),
    ],
)
def test_disk_usage_debug_returns_text_then_stderr(
    tmp_path: Path, verbose: bool, expected: list[str]
) -> None:
    client, log = make_fake_cli(
        tmp_path, [Rule(match=("du",), stdout="ID  SIZE\n", stderr="debug log\n")]
    )
    assert (
        client.buildx.disk_usage(verbose=verbose, debug=True) == "ID  SIZE\ndebug log"
    )
    assert calls(log) == [expected]


def test_disk_usage_rejects_malformed_records(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("du",), stdout="not json\n")])
    with pytest.raises(ParsingError):
        client.buildx.disk_usage()


def test_disk_usage_validates_timeout_before_filters_and_launch(
    tmp_path: Path,
) -> None:
    client, log = make_fake_cli(tmp_path)
    consumed: list[str] = []

    def pairs() -> Iterator[tuple[str, str]]:
        consumed.append("started")
        yield ("until", "1h")

    with pytest.raises(ValueError):
        client.buildx.disk_usage(filters=pairs(), timeout=0)
    assert consumed == []
    assert calls(log) == []


def test_disk_usage_deadline_raises_docker_timeout(tmp_path: Path) -> None:
    client, _ = make_fake_cli(tmp_path, [Rule(match=("du",), sleep=30)])
    with pytest.raises(DockerTimeout):
        client.buildx.disk_usage(timeout=0.5)


def test_new_calls_inherit_non_default_client_settings(tmp_path: Path) -> None:
    client_call, log = fake_cli_call(tmp_path)
    client = DockerClient(client_call=client_call, context="remote", host="tcp://h:1")
    client.buildx.disk_usage(builder="b")
    assert calls(log) == [
        [
            "--context",
            "remote",
            "--host",
            "tcp://h:1",
            "buildx",
            "du",
            "--format",
            "json",
            "--builder",
            "b",
        ]
    ]
