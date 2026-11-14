"""Static contracts for Docker CLI 29 context commands; checked by ty, not pytest."""

from pathlib import Path
from typing import assert_type

from pywhales import Context, DockerClient, DockerContextConfig


def _check_context_show(client: DockerClient) -> None:
    assert_type(client.context.show(), str)


def _check_context_archives(
    client: DockerClient, context: Context, archive: Path
) -> None:
    assert_type(client.context.export("ctx"), None)
    assert_type(client.context.export(context, archive, timeout=5), None)
    assert_type(client.context.export("ctx", "ctx.tar", timeout=None), None)
    assert_type(client.context.import_("ctx", archive), Context)
    assert_type(client.context.import_("ctx", "ctx.tar", timeout=5), Context)


def _check_context_update(
    client: DockerClient, context: Context, endpoint: DockerContextConfig
) -> None:
    assert_type(client.context.update("ctx"), Context)
    assert_type(client.context.update(context, "description", endpoint), Context)
    assert_type(client.context.update("ctx", docker={"host": "ssh://h"}), Context)
    assert_type(context.update(), None)
    assert_type(context.update("description", endpoint), None)
    assert_type(context.update(docker={"host": "ssh://h"}), None)
