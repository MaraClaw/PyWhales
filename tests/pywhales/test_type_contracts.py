"""Static contracts for resource, process, image, and Buildx return shapes.

The ty check analyzes assert_type calls inside _check_* functions. Pytest does
not collect or execute these functions; they are not runtime Docker examples.
"""

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import assert_type

from pywhales import (
    Config,
    Container,
    DockerClient,
    Image,
    Network,
    Node,
    Secret,
    Service,
    Volume,
)
from pywhales.client_config import ClientConfig
from pywhales.components.buildx.cli_wrapper import BakeResult
from pywhales.components.buildx.imagetools.models import Manifest
from pywhales.components.compose.models import ComposeEvent
from pywhales.components.config.cli_wrapper import ConfigListFilter
from pywhales.components.image.streams import EmptyImageStream, ImageStream
from pywhales.components.system.cli_wrapper import SystemPruneFilter
from pywhales.components.system.models import DockerEvent
from pywhales.components.top import ComposeTopResult, ContainerTopResult
from pywhales.utils import MappedProcessStream, ProcessStream, stream_stdout_and_stderr


def _check_strict_environment_setting(flag: bool) -> None:
    config = ClientConfig(strict_env_transport=flag)
    assert_type(config.strict_env_transport, bool)
    client = DockerClient(strict_env_transport=flag)
    assert_type(client.client_config.strict_env_transport, bool)


def _check_resource_inspection_contracts(client: DockerClient) -> None:
    assert_type(client.container.inspect("container"), Container)
    assert_type(client.container.inspect(["container"]), list[Container])
    assert_type(client.container.inspect(iter(["container"])), list[Container])
    assert_type(client.image.inspect("image"), Image)
    assert_type(client.image.inspect(["image"]), list[Image])
    assert_type(client.image.inspect(iter(["image"])), list[Image])
    assert_type(client.network.inspect("network"), Network)
    assert_type(client.network.inspect(["network"]), list[Network])
    assert_type(client.network.inspect(iter(["network"])), list[Network])
    assert_type(client.volume.inspect("volume"), Volume)
    assert_type(client.volume.inspect(["volume"]), list[Volume])
    assert_type(client.volume.inspect(iter(["volume"])), list[Volume])
    assert_type(client.service.inspect("service"), Service)
    assert_type(client.service.inspect(["service"]), list[Service])
    assert_type(client.node.inspect("node"), Node)
    assert_type(client.node.inspect(["node"]), list[Node])
    assert_type(client.config.inspect("config"), Config)
    assert_type(client.config.inspect(["config"]), list[Config])
    assert_type(client.secret.inspect("secret"), Secret)
    assert_type(client.secret.inspect(["secret"]), list[Secret])
    assert_type(client.secret.inspect(iter(["secret"])), list[Secret])


def _check_process_contracts(client: DockerClient, raw: bool) -> None:
    assert_type(client.top("container"), ContainerTopResult)
    assert_type(
        client.container.top("container", ["-eo", "pid,args"]), ContainerTopResult
    )
    assert_type(client.top("container", raw=True), str)
    assert_type(client.top("container", raw=raw), ContainerTopResult | str)
    assert_type(client.compose.top(), ComposeTopResult)
    assert_type(client.compose.top(["service"], raw=True), str)
    assert_type(client.compose.top([], raw=raw), ComposeTopResult | str)
    assert_type(client.system.events(), MappedProcessStream[DockerEvent])
    assert_type(client.compose.events(), MappedProcessStream[ComposeEvent])
    with stream_stdout_and_stderr(["command"]) as stream:
        assert_type(stream, ProcessStream)
        assert_type(next(stream), tuple[str, bytes])
        assert_type(stream.close(), None)
    with client.build(".", stream_logs=True) as logs:
        assert_type(logs, MappedProcessStream[str])
        assert_type(next(logs), str)
        assert_type(logs.close(), None)


def _check_owned_stream_contracts(client: DockerClient, flag: bool) -> None:
    assert_type(client.container.prune(stream_logs=flag), ProcessStream | None)
    assert_type(client.run("image", stream=flag), Container | str | ProcessStream)
    with client.run("image", stream=True) as stream:
        assert_type(stream, ProcessStream)
        assert_type(next(stream), tuple[str, bytes])
        assert_type(stream.close(), None)
    with client.service.logs("service", stream=True) as stream:
        assert_type(stream, ProcessStream)
        assert_type(next(stream), tuple[str, bytes])
        assert_type(stream.close(), None)
    assert_type(client.compose.build(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.create(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.down(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.pull(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.start(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.stop(stream_logs=flag), ProcessStream | None)
    assert_type(client.compose.up(stream_logs=flag), ProcessStream | None)
    with client.compose.logs(stream=True) as stream:
        assert_type(stream, ProcessStream)
        assert_type(next(stream), tuple[str, bytes])
        assert_type(stream.close(), None)
    with client.compose.run("app", tty=False, stream=True) as stream:
        assert_type(stream, ProcessStream)
        assert_type(next(stream), tuple[str, bytes])
        assert_type(stream.close(), None)
    for optional in (
        client.container.prune(stream_logs=True),
        client.compose.build(stream_logs=True),
        client.compose.create(stream_logs=True),
        client.compose.down(stream_logs=True),
        client.compose.pull(stream_logs=True),
        client.compose.start(stream_logs=True),
        client.compose.stop(stream_logs=True),
        client.compose.up(stream_logs=True),
    ):
        if optional is not None:
            with optional as stream:
                assert_type(stream, ProcessStream)
                assert_type(next(stream), tuple[str, bytes])
                assert_type(stream.close(), None)


def _check_image_return_contracts(
    client: DockerClient, stream: bool, output: Path | None
) -> None:
    assert_type(client.pull("busybox"), Image)
    assert_type(client.pull(["busybox"]), list[Image])
    assert_type(client.pull("busybox", stream_logs=True), ImageStream)
    assert_type(client.pull("busybox", False, True), ImageStream)
    assert_type(
        client.pull("busybox", stream_logs=stream),
        Image | ImageStream,
    )
    assert_type(
        client.pull(["busybox"], stream_logs=stream),
        list[Image] | ImageStream | EmptyImageStream,
    )
    assert_type(client.push("busybox"), None)
    assert_type(client.push("busybox", stream_logs=True), ImageStream)
    assert_type(client.push([], stream_logs=True), ImageStream | None)
    with client.pull("busybox", stream_logs=True) as logs:
        assert_type(next(logs), tuple[str, bytes])
        assert_type(logs.close(), None)
    with client.pull([], stream_logs=True) as empty:
        assert_type(next(empty), tuple[str, bytes])
        assert_type(empty.close(), None)
    with client.push("busybox", stream_logs=True) as logs:
        assert_type(next(logs), tuple[str, bytes])
        assert_type(logs.close(), None)
    assert_type(client.image.save("busybox"), Iterator[bytes])
    assert_type(client.image.save("busybox", output=Path("image.tar")), None)
    assert_type(client.image.save("busybox", output=output), Iterator[bytes] | None)
    assert_type(client.image.load(iter([b"archive"])), list[str])


def _check_buildx_return_contracts(client: DockerClient, stream: bool) -> None:
    assert_type(client.build("."), Image | None)
    assert_type(client.build(".", load=True), Image | None)
    assert_type(client.build(".", stream_logs=True), MappedProcessStream[str])
    assert_type(
        client.build(".", stream_logs=stream), Image | MappedProcessStream[str] | None
    )
    assert_type(client.buildx.bake(), BakeResult)
    assert_type(client.buildx.bake(stream_logs=True), MappedProcessStream[str])
    assert_type(
        client.buildx.bake(stream_logs=stream), BakeResult | MappedProcessStream[str]
    )
    assert_type(client.buildx.prune(), None)
    assert_type(client.buildx.prune(stream_logs=True), MappedProcessStream[str])
    assert_type(
        client.buildx.prune(stream_logs=stream), MappedProcessStream[str] | None
    )
    assert_type(client.buildx.imagetools.create(), None)
    assert_type(client.buildx.imagetools.create(dry_run=True), Manifest)
    assert_type(client.buildx.imagetools.create(dry_run=stream), Manifest | None)


def _check_mapping_or_pair_filter_contracts(
    client: DockerClient,
    mapping: Mapping[str, str],
    pairs: Iterable[tuple[str, str]],
    config_pairs: Iterable[ConfigListFilter],
    prune_pairs: Iterable[SystemPruneFilter],
    stream: bool,
) -> None:
    assert_type(client.config.list(), list[Config])
    assert_type(client.config.list({"name": "app"}), list[Config])
    assert_type(client.config.list(filters=mapping), list[Config])
    assert_type(client.config.list(MappingProxyType({"id": "x"})), list[Config])
    assert_type(
        client.config.list(filters=[("name", "a"), ("label", "b"), ("id", "c")]),
        list[Config],
    )
    assert_type(client.config.list(filters=(("name", "a"),)), list[Config])
    assert_type(client.config.list(filters=config_pairs), list[Config])
    assert_type(
        client.config.list(filters=(("name", name) for name in ["a", "b"])),
        list[Config],
    )

    assert_type(client.system.events(), MappedProcessStream[DockerEvent])
    assert_type(
        client.system.events(filters={"type": "container"}),
        MappedProcessStream[DockerEvent],
    )
    assert_type(client.system.events(filters=mapping), MappedProcessStream[DockerEvent])
    assert_type(
        client.system.events(filters=[("event", "start"), ("event", "die")]),
        MappedProcessStream[DockerEvent],
    )
    assert_type(client.system.events(filters=pairs), MappedProcessStream[DockerEvent])
    assert_type(
        client.system.events(None, None, iter([("type", "container")])),
        MappedProcessStream[DockerEvent],
    )

    assert_type(client.system.prune(), None)
    assert_type(client.system.prune(filters={"until": "24h"}), None)
    assert_type(client.system.prune(filters=mapping), None)
    assert_type(
        client.system.prune(
            filters=[("label!", "keep"), ("label", "tmp"), ("until", "24h")]
        ),
        None,
    )
    assert_type(client.system.prune(filters=prune_pairs), None)
    assert_type(client.system.prune(True, True, (("until", "1h"),)), None)

    assert_type(client.buildx.prune(filters={"until": "24h"}), None)
    assert_type(client.buildx.prune(filters=mapping), None)
    assert_type(client.buildx.prune(filters=[("until", "24h"), ("type", "x")]), None)
    assert_type(client.buildx.prune(filters=pairs), None)
    assert_type(
        client.buildx.prune(filters={"until": "24h"}, stream_logs=True),
        MappedProcessStream[str],
    )
    assert_type(
        client.buildx.prune(filters=[("until", "24h")], stream_logs=True),
        MappedProcessStream[str],
    )
    assert_type(
        client.buildx.prune(True, [("until", "24h")], False),
        None,
    )
    assert_type(
        client.buildx.prune(filters=mapping, stream_logs=stream),
        MappedProcessStream[str] | None,
    )
    assert_type(
        client.buildx.prune(filters=pairs, stream_logs=stream),
        MappedProcessStream[str] | None,
    )
