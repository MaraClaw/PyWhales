"""Static stack config, deploy, remove, ps and services contracts, checked by ty."""

from collections.abc import Iterator
from pathlib import Path
from typing import assert_type

from pywhales import DockerClient, Service, Stack, Task
from pywhales.components.service.cli_wrapper import ServiceListFilter
from pywhales.components.stack.cli_wrapper import StackPsFilter


def _check_stack_config(client: DockerClient) -> None:
    assert_type(client.stack.config("a.yml"), str)
    assert_type(client.stack.config([Path("a.yml"), "b.yml"]), str)
    assert_type(client.stack.config("a.yml", skip_interpolation=True), str)


def _check_stack_deploy(client: DockerClient) -> None:
    assert_type(client.stack.deploy("web", "a.yml"), Stack)
    assert_type(client.stack.deploy("web", "a.yml", detach=None, quiet=False), Stack)
    assert_type(client.stack.deploy("web", "a.yml", detach=True, quiet=True), Stack)
    assert_type(
        client.stack.deploy("web", ["a.yml"], detach=False, timeout=30.0),
        Stack,
    )


def _check_stack_remove(client: DockerClient, stack: Stack) -> None:
    assert_type(client.stack.remove("web"), None)
    assert_type(client.stack.remove(["web", stack], detach=False), None)
    assert_type(stack.remove(), None)
    assert_type(stack.remove(detach=True), None)


def _check_stack_listings(
    client: DockerClient,
    stack: Stack,
    task_pairs: Iterator[StackPsFilter],
    service_pairs: Iterator[ServiceListFilter],
) -> None:
    assert_type(client.stack.ps("web"), list[Task])
    assert_type(
        client.stack.ps(stack, filters=[("desired-state", "running"), ("node", "n1")]),
        list[Task],
    )
    assert_type(client.stack.ps("web", filters={"name": "web_app"}), list[Task])
    assert_type(client.stack.ps("web", filters=task_pairs), list[Task])
    assert_type(stack.ps(filters=[("id", "abc")]), list[Task])
    assert_type(client.stack.services("web"), list[Service])
    assert_type(
        client.stack.services(stack, filters=[("mode", "global"), ("label", "a=b")]),
        list[Service],
    )
    assert_type(
        client.stack.services("web", filters={"name": "web_app"}), list[Service]
    )
    assert_type(client.stack.services("web", filters=service_pairs), list[Service])
    assert_type(stack.services(filters=[("name", "web_app")]), list[Service])
