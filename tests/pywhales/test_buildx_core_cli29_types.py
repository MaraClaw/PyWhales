"""Static Buildx core contracts for disk usage, debug text modes and new options.

The ty check analyzes these _check_* bodies without executing their Docker calls.
Pytest does not collect the functions.
"""

from datetime import timedelta
from typing import Literal, assert_type

from pywhales import DockerClient, Image
from pywhales.components.buildx.cli_wrapper import BakeResult, Builder
from pywhales.components.buildx.dap import BuildxDapCLI
from pywhales.components.buildx.history import BuildxHistoryCLI
from pywhales.components.buildx.models import BuildxDiskUsage
from pywhales.components.buildx.policy import BuildxPolicyCLI
from pywhales.components.buildx.replay import BuildxReplayCLI
from pywhales.utils import MappedProcessStream


def _check_disk_usage(client: DockerClient, flag: bool) -> None:
    assert_type(client.buildx.disk_usage(), list[BuildxDiskUsage])
    assert_type(
        client.buildx.disk_usage(
            filters={"until": "24h"},
            builder="b",
            status_timeout=timedelta(seconds=5),
            timeout=30,
        ),
        list[BuildxDiskUsage],
    )
    assert_type(
        client.buildx.disk_usage(filters=[("type", "regular")]), list[BuildxDiskUsage]
    )
    assert_type(client.buildx.disk_usage(verbose=False), list[BuildxDiskUsage])
    assert_type(client.buildx.disk_usage(verbose=True), str)
    assert_type(client.buildx.disk_usage(debug=True), str)
    assert_type(client.buildx.disk_usage(verbose=True, debug=True), str)
    assert_type(client.buildx.disk_usage(verbose=flag), list[BuildxDiskUsage] | str)
    assert_type(client.buildx.disk_usage(debug=flag), list[BuildxDiskUsage] | str)


def _check_debug_text_modes(client: DockerClient, flag: bool) -> None:
    assert_type(client.buildx.list(), list[Builder])
    assert_type(client.buildx.list(status_timeout=3), list[Builder])
    assert_type(client.buildx.list(debug=False), list[Builder])
    assert_type(client.buildx.list(debug=True), str)
    assert_type(client.buildx.list(debug=flag), list[Builder] | str)
    assert_type(client.buildx.inspect(), Builder)
    assert_type(client.buildx.inspect("b", True, status_timeout=3), Builder)
    assert_type(client.buildx.inspect(debug=True), str)
    assert_type(client.buildx.inspect(debug=flag), Builder | str)


def _check_build_options(client: DockerClient) -> None:
    assert_type(
        client.buildx.build(
            ".",
            cgroup_parent="/p",
            policies=["a"],
            quiet=True,
            resources={"memory": "2g", "cpu-quota": 50000},
            shm_size=64,
            ulimits="nofile=1:2",
            debug=True,
        ),
        Image | None,
    )
    assert_type(client.buildx.build(".", call="check", shm_size="64m", quiet=True), str)
    assert_type(
        client.buildx.build(".", stream_logs=True, ulimits=["a"], debug=True),
        MappedProcessStream[str],
    )


def _check_bake_options(
    client: DockerClient, kind: Literal["targets", "variables"] | None, stream: bool
) -> None:
    assert_type(
        client.buildx.bake(
            allow="network.host",
            policies=["a"],
            provenance={"mode": "max"},
            sbom=True,
            variables_set={"TAG": "1"},
            debug=True,
        ),
        BakeResult,
    )
    assert_type(client.buildx.bake(list="targets"), str)
    assert_type(client.buildx.bake(list="variables", return_config=False), str)
    assert_type(
        client.buildx.bake(list="targets", stream_logs=True), MappedProcessStream[str]
    )
    assert_type(client.buildx.bake(list=kind), BakeResult | str)
    assert_type(
        client.buildx.bake(list="targets", stream_logs=stream),
        str | MappedProcessStream[str],
    )


def _check_lifecycle_options(
    client: DockerClient, builder: Builder, leave: bool
) -> None:
    assert_type(
        client.buildx.create(
            name="n",
            config="buildkitd.toml",
            node="n2",
            status_timeout=5,
            debug=True,
        ),
        Builder,
    )
    assert_type(client.buildx.create(name="n", leave=False), Builder)
    assert_type(client.buildx.create(name="n", node="n2", leave=True), None)
    assert_type(client.buildx.create(name="n", leave=leave), Builder | None)
    assert_type(client.buildx.remove(), None)
    assert_type(client.buildx.remove("b", force=True, keep_state=True), None)
    assert_type(
        client.buildx.remove(builder, status_timeout=timedelta(seconds=1)), None
    )
    assert_type(client.buildx.remove(all_inactive=True, force=True, debug=True), None)
    assert_type(builder.remove(keep_state=True, force=True, debug=True), None)
    assert_type(client.buildx.stop("b", debug=True), None)
    assert_type(client.buildx.use("b", debug=True), None)
    assert_type(client.buildx.version(debug=True), str)


def _check_prune_options(client: DockerClient, stream: bool) -> None:
    assert_type(
        client.buildx.prune(
            max_used_space="1GB",
            min_free_space=1,
            reserved_space="1GB",
            verbose=True,
            status_timeout=timedelta(seconds=2),
            debug=True,
            timeout=5,
        ),
        None,
    )
    assert_type(
        client.buildx.prune(stream_logs=True, verbose=True), MappedProcessStream[str]
    )
    assert_type(
        client.buildx.prune(stream_logs=stream, debug=True),
        MappedProcessStream[str] | None,
    )


def _check_interactive(client: DockerClient) -> None:
    assert_type(client.buildx.dap, BuildxDapCLI)
    assert_type(client.buildx.history, BuildxHistoryCLI)
    assert_type(client.buildx.policy, BuildxPolicyCLI)
    assert_type(client.buildx.replay, BuildxReplayCLI)
    assert_type(client.buildx.dap.build("."), None)
    assert_type(
        client.buildx.dap.build(
            ".",
            build_args={"A": "1"},
            iidfile="id",
            quiet=True,
            call="check",
            debug=True,
            timeout=10,
        ),
        None,
    )
    assert_type(client.buildx.dial_stdio(), None)
    assert_type(
        client.buildx.dial_stdio(
            builder="b", platform="linux/amd64", progress="plain", debug=True, timeout=5
        ),
        None,
    )
