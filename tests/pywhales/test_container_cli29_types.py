"""Static contracts for Docker CLI 29 container options; checked by ty, not pytest."""

from datetime import timedelta
from pathlib import Path
from typing import assert_type

from pywhales import Container, DockerClient, Image
from pywhales.components.container.cli_wrapper import PublishedPort
from pywhales.components.system.gpu import (
    GpuRequest,
    GpuSupport,
    check_generic_resources,
    compose_gpu_requests,
    generic_resource_requests,
)
from pywhales.utils import ProcessStream


def _check_create_and_run(client: DockerClient) -> None:
    assert_type(
        client.container.create(
            "alpine",
            annotations={"k": "v"},
            cpu_count=1,
            cpu_percent=50,
            io_maxbandwidth="10mb",
            io_maxiops=100,
            quiet=True,
            umask="022",
            use_api_socket=True,
        ),
        Container,
    )
    assert_type(
        client.container.run(
            "alpine",
            annotations={"k": "v"},
            attach=["stdout", "stderr"],
            cpu_count=1,
            cpu_percent=50,
            detach_keys="ctrl-x",
            io_maxbandwidth=1024,
            io_maxiops=100,
            quiet=True,
            umask="022",
            use_api_socket=True,
        ),
        str,
    )
    assert_type(client.container.run("alpine", attach="stdin", detach=True), Container)
    assert_type(
        client.container.run("alpine", attach=("stdout",), stream=True), ProcessStream
    )


def _check_commit_and_copy(client: DockerClient, container: Container) -> None:
    assert_type(client.container.commit("web", changes="ENV A=1", pause=False), Image)
    assert_type(container.commit("t", changes=["ENV A=1"], pause=False), Image)
    assert_type(
        client.container.copy(
            "src", ("web", "/dst"), archive=True, follow_link=True, quiet=True
        ),
        None,
    )
    assert_type(container.copy_from("/src", "dst", archive=True, quiet=True), None)
    assert_type(container.copy_to("src", "/dst", follow_link=True), None)


def _check_inspect_list_remove(client: DockerClient, container: Container) -> None:
    assert_type(client.container.inspect("web", size=True), Container)
    assert_type(client.container.inspect(["web"], size=True), list[Container])
    assert_type(client.container.list(last=2, latest=True), list[Container])
    assert_type(client.container.remove("web", link=True), None)
    assert_type(container.remove(link=True), None)


def _check_stop_restart_start(client: DockerClient, container: Container) -> None:
    assert_type(client.container.stop("web", 5, signal="SIGKILL"), None)
    assert_type(client.container.stop("web", timedelta(seconds=5), signal=9), None)
    assert_type(client.container.restart("web", 5, signal="SIGHUP"), None)
    assert_type(container.stop(5, signal="SIGKILL"), None)
    assert_type(container.restart(timedelta(seconds=5), signal=1), None)
    assert_type(
        client.container.start("web", checkpoint="cp", checkpoint_dir=Path("x")), None
    )
    assert_type(client.container.start("web", True, checkpoint="cp"), str)
    assert_type(
        client.container.start("web", True, stream=True, checkpoint_dir="x"),
        ProcessStream,
    )
    assert_type(container.start(checkpoint="cp", checkpoint_dir="x"), None)


def _check_port(client: DockerClient, container: Container) -> None:
    assert_type(client.container.port("web"), list[PublishedPort])
    assert_type(client.container.port("web", 80, "tcp"), list[PublishedPort])
    assert_type(container.port("80/tcp"), list[PublishedPort])
    binding = container.port()[0]
    assert_type(binding.container_port, str)
    assert_type(binding.host_ip, str)
    assert_type(binding.host_port, int)


def _check_gpu_preflight(client: DockerClient) -> None:
    assert_type(
        client.container.create("cuda", gpus="all", require_gpu=True), Container
    )
    assert_type(client.run("cuda", gpus=1, require_gpu=True), str)
    assert_type(
        client.run("cuda", devices=["nvidia.com/gpu=0"], detach=True, require_gpu=True),
        Container,
    )
    support = client.system.gpu_support()
    assert_type(support, GpuSupport)
    assert_type(support.whole_gpus, tuple[str, ...])
    assert_type(support.gpu_uuids, tuple[str, ...])
    assert_type(support.mig_devices, tuple[str, ...])
    assert_type(support.gpu_count, int | None)
    assert_type(GpuRequest.parse("device=0"), GpuRequest | None)
    assert_type(GpuRequest.parse(2), GpuRequest | None)
    assert_type(support.check_requests([GpuRequest(count=1)], ["/dev/fuse"]), None)
    assert_type(
        compose_gpu_requests({}), tuple[tuple[GpuRequest, ...], tuple[str, ...]]
    )
    assert_type(generic_resource_requests(["gpu=1"]), dict[str, int])
    assert_type(check_generic_resources(["gpu=1"], client.node.list()), None)
