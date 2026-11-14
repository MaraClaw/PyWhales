"""Static contracts for Docker CLI 29 plugin options; checked by ty, not pytest."""

from typing import assert_type

from pywhales import DockerClient, Plugin


def _check_plugin_list(client: DockerClient) -> None:
    assert_type(client.plugin.list(), list[Plugin])
    assert_type(client.plugin.list(None), list[Plugin])
    assert_type(client.plugin.list([("enabled", "true")]), list[Plugin])
    assert_type(client.plugin.list([("capability", "volumedriver")]), list[Plugin])
    assert_type(client.plugin.list({"enabled": "false"}), list[Plugin])


def _check_plugin_content_trust(client: DockerClient, plugin: Plugin) -> None:
    assert_type(client.plugin.install("name", disable_content_trust=True), Plugin)
    assert_type(client.plugin.push(plugin, disable_content_trust=True), None)
    assert_type(client.plugin.upgrade(plugin, disable_content_trust=True), None)
    assert_type(plugin.push(True, timeout=5), None)
    assert_type(plugin.upgrade(None, True), None)
