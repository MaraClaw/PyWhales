"""Fixtures for documentation provider mocks and Pydantic adapter counting."""

from collections.abc import Callable, Sequence
from typing import Any
from unittest.mock import MagicMock

import pydantic
import pytest
from pytest_mock import MockerFixture

import pywhales.utils
from docs import docs_utils
from pywhales import Builder, Container, Volume
from pywhales.components.system.models import SystemInfo
from pywhales.exceptions import DockerException, NoSuchImage


@pytest.fixture
def daemon(mocker: MockerFixture) -> MagicMock:
    """Replace documentation Docker access with stateful resource mocks.

    Context-managed resources record removal, inventory helpers use mock state,
    and subprocess.run rejects accidental external commands. No real daemon is
    started or contacted; pytest restores patches after each test.

    Args:
        mocker: Installs scoped replacements for providers and subprocess access.

    Returns:
        A mutable client mock exposing named example resources and acquisitions.
    """
    client = MagicMock()
    mocker.patch.object(docs_utils, "docker", client)
    for kind in (
        "network",
        "node",
        "service",
        "config",
        "volume",
        "builder",
        "image",
        "plugin",
        "container",
    ):
        resource = MagicMock(name=kind)
        resource.id = f"{kind}-id"
        resource.name = f"{kind}-name"
        resource.remove.return_value = None
        resource.__enter__.return_value = resource
        resource.__exit__.side_effect = lambda *args, resource=resource: (
            resource.remove()
        )
        client.attach_mock(resource, f"example_{kind}")
    client.system.info.return_value = SystemInfo.model_validate(
        {"Swarm": {"LocalNodeState": "inactive"}}
    )
    client.node.list.return_value = [client.example_node]
    for kind in ("network", "service", "config", "volume"):
        getattr(client, kind).create.return_value = getattr(client, f"example_{kind}")
    client.buildx.create.return_value = client.example_builder
    client.run.return_value = client.example_container
    client.image.inspect.side_effect = NoSuchImage(["docker", "image", "inspect"], 1)
    client.image.pull.return_value = client.example_image
    client.image.exists.return_value = True
    client.plugin.list.return_value = []
    client.plugin.install.return_value = client.example_plugin
    client.plugin.inspect.side_effect = DockerException(
        ["docker", "plugin", "inspect"], 1
    )

    def inventory(
        command: Sequence[str], reference: Callable[[str], Builder | Container | Volume]
    ) -> dict[str, MagicMock]:
        resources = getattr(client, command[0]).list()
        return {resource.name: resource for resource in resources}

    def names(command: Sequence[str]) -> list[str]:
        if command[0] == "image":
            return (
                ["ubuntu:latest", "busybox:latest"]
                if client.image.exists.return_value
                else []
            )
        return [resource.name for resource in client.plugin.list()]

    mocker.patch.object(docs_utils, "_example_inventory", side_effect=inventory)
    mocker.patch.object(docs_utils, "_example_names", side_effect=names)
    mocker.patch("subprocess.run", side_effect=AssertionError("external command"))
    return client


@pytest.fixture
def adapter_constructions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record Pydantic adapter constructions against an empty shared cache.

    Replaces ``pydantic.TypeAdapter`` with a function that appends each requested
    type to the returned list before building the real adapter, and swaps in an
    empty ``pywhales.utils`` adapter cache so first use is observable. It forwards
    only the type, and adapters built through a ``TypeAdapter`` name imported from
    Pydantic before the patch aren't counted. pytest restores both replacements
    after the test.

    Args:
        monkeypatch: Installs the counting adapter class and the empty cache.

    Returns:
        The constructed types in construction order, unhashable ones included.
    """
    constructed: list[Any] = []
    build = pydantic.TypeAdapter

    def counting(type_: type[Any]) -> pydantic.TypeAdapter[Any]:
        constructed.append(type_)
        return build(type_)

    monkeypatch.setattr(pydantic, "TypeAdapter", counting)
    monkeypatch.setattr(pywhales.utils, "_ADAPTERS", {})
    return constructed
