"""Static contracts for the Compose v5.7.1 surface; checked by ty, not pytest."""

from datetime import timedelta
from pathlib import Path
from typing import Literal, assert_type

from pydantic import JsonValue

from pywhales import Container, DockerClient, Image
from pywhales.client_config import ClientConfig
from pywhales.components.compose.bridge import (
    ComposeBridgeCLI,
    ComposeTransformationsCLI,
)
from pywhales.components.compose.models import (
    ComposeConfig,
    ComposeImage,
    ComposeStats,
    ComposeTransformation,
    ComposeVariable,
    ComposeVolume,
)
from pywhales.utils import ProcessStream


def _check_globals(config: ClientConfig) -> None:
    assert_type(config.compose_all_resources, bool)
    assert_type(config.compose_ansi, Literal["never", "always", "auto"] | None)
    assert_type(ClientConfig(compose_all_resources=True).compose_all_resources, bool)
    assert_type(
        ClientConfig(compose_ansi="never").compose_ansi,
        Literal["never", "always", "auto"] | None,
    )
    client = DockerClient(compose_all_resources=True, compose_ansi="auto")
    assert_type(
        client.client_config.compose_ansi, Literal["never", "always", "auto"] | None
    )


def _check_new_commands(client: DockerClient) -> None:
    compose = client.compose
    assert_type(compose.attach("app", stdin=False, timeout=3), None)
    assert_type(compose.commit("app", "repo:tag", pause=False, timeout=5), Image)
    assert_type(compose.cp(("app", "/a"), Path("a"), index=2), None)
    assert_type(compose.cp("a", ("app", Path("/a")), all=True), None)
    assert_type(compose.export("app", "app.tar", index=1, timeout=5), None)
    assert_type(compose.images(), list[ComposeImage])
    assert_type(compose.images(["app"]), list[ComposeImage])
    assert_type(compose.scale({"app": 2}, no_deps=True, timeout=5), None)
    assert_type(compose.volumes("app"), list[ComposeVolume])
    assert_type(compose.wait(["job"], down_project=True, timeout=5), int)


def _check_stats(client: DockerClient, stream: bool) -> None:
    compose = client.compose
    assert_type(compose.stats(), list[ComposeStats])
    assert_type(compose.stats("app", all=True, aggregate=True), list[ComposeStats])
    assert_type(compose.stats(stream=False, timeout=5), list[ComposeStats])
    assert_type(compose.stats(stream=True), ProcessStream)
    assert_type(compose.stats(all_projects=True, stream=True), ProcessStream)
    assert_type(compose.stats(stream=stream), list[ComposeStats] | ProcessStream)


def _check_bridge(client: DockerClient) -> None:
    bridge = client.compose.bridge
    assert_type(bridge, ComposeBridgeCLI)
    assert_type(bridge.transformations, ComposeTransformationsCLI)
    assert_type(
        bridge.convert(output="out", transformations=["a", "b"], yes=True), None
    )
    assert_type(bridge.convert(templates=Path("tpl"), transformations="a"), None)
    assert_type(bridge.transformations.list(), list[ComposeTransformation])
    assert_type(bridge.transformations.create("dir", from_="base", timeout=5), None)


def _check_config(client: DockerClient, quiet: bool) -> None:
    compose = client.compose
    assert_type(compose.config(), ComposeConfig)
    assert_type(compose.config(no_interpolate=True, no_normalize=True), ComposeConfig)
    assert_type(compose.config(True, lock_image_digests=True), dict[str, JsonValue])
    assert_type(compose.config(quiet=True), None)
    assert_type(compose.config(output="out.yaml"), None)
    assert_type(compose.config(True, output=Path("out.json"), quiet=quiet), None)
    assert_type(
        compose.config(quiet=quiet), ComposeConfig | dict[str, JsonValue] | None
    )
    assert_type(compose.config_names("services"), list[str])
    assert_type(compose.config_hashes(), dict[str, str])
    assert_type(compose.config_hashes(["app", "job"]), dict[str, str])
    assert_type(compose.config_variables(), list[ComposeVariable])
    assert_type(compose.config_environment(), dict[str, str])


def _check_ps(client: DockerClient, names: bool) -> None:
    compose = client.compose
    assert_type(compose.ps(), list[Container])
    assert_type(compose.ps(filters={"status": "running"}), list[Container])
    assert_type(
        compose.ps(status=["running", "exited"], orphans=False), list[Container]
    )
    assert_type(compose.ps(service_names=False), list[Container])
    assert_type(compose.ps(service_names=True), list[str])
    assert_type(
        compose.ps(["app"], True, status="paused", service_names=True), list[str]
    )
    assert_type(compose.ps(service_names=names), list[Container] | list[str])


def _check_existing_commands_with_new_options(client: DockerClient) -> None:
    compose = client.compose
    assert_type(compose.build("app", memory="512m"), None)
    assert_type(compose.create("app", scales={"app": 2}, yes=True), None)
    assert_type(compose.kill("app", remove_orphans=True), None)
    assert_type(compose.logs("app", index=1), str)
    assert_type(compose.logs("app", stream=True, index=1), ProcessStream)
    assert_type(compose.publish("repo", app=True), None)
    assert_type(compose.pull("app", ignore_buildable=True, policy="always"), None)
    assert_type(
        compose.push(["app"], ignore_missing=True, include_deps=True, quiet=True), None
    )
    assert_type(compose.restart("app", no_deps=True), None)
    assert_type(compose.start("app", wait=True, wait_timeout=30), None)
    assert_type(compose.version(short=True), str)
    assert_type(
        compose.up(
            "app",
            always_recreate_deps=True,
            attach_services=["app"],
            menu=False,
            quiet_build=True,
            quiet_pull=True,
            shutdown_timeout=timedelta(seconds=5),
            timestamps=True,
        ),
        None,
    )
    assert_type(compose.up(stream_logs=True, menu=True), ProcessStream | None)


def _check_run(client: DockerClient) -> None:
    result = client.compose.run(
        "app",
        tty=False,
        cap_add=["NET_ADMIN"],
        cap_drop=["MKNOD"],
        env_from_files=[Path("a.env"), "b.env"],
        interactive=False,
        no_recreate=True,
        pull="always",
        quiet=True,
        quiet_build=True,
        quiet_pull=True,
        remove_orphans=True,
    )
    assert_type(result, str)
