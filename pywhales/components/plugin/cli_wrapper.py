"""Daemon-plugin handles, privileged installation, and CLI configuration."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from types import MappingProxyType, TracebackType
from typing import Literal, Self, overload, override

from pydantic import JsonValue

from pywhales._inspect_validation import parse_namespace_inspect_results
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.plugin.models import (
    PluginConfig,
    PluginInspectResult,
    PluginSettings,
)
from pywhales.exceptions import NoSuchPlugin
from pywhales.utils import (
    ValidPath,
    format_filters_for_cli,
    run,
    to_list,
    validate_timeout,
)

type PluginListFilter = (
    tuple[Literal["capability"], str] | tuple[Literal["enabled"], str]
)


def _require_content_trust_skipped(disable_content_trust: bool) -> None:
    """Reject enabling content trust, which Docker CLI 29 plugin commands lack.

    The CLI no longer verifies or signs plugin images, so the former
    `--disable-content-trust` option is gone and `True` is a no-op.

    Raises:
        ValueError: If `disable_content_trust` is false.
    """
    if not disable_content_trust:
        raise ValueError("Docker CLI 29 plugin commands cannot enable content trust")


class Plugin(ReloadableObjectFromJson[PluginInspectResult]):
    """Reloadable daemon plugin handle with forced context-exit removal.

    Context-manager cleanup is attempted even if the body raises, and ignores a
    plugin that is already gone. Other removal failures are grouped with any
    active body exception.
    """

    def __init__(
        self, client_config: ClientConfig, reference: str, is_immutable_id: bool = False
    ) -> None:
        super().__init__(client_config, "id", reference, is_immutable_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        with resource_cleanup(exc_val, "Plugin body and cleanup both failed"):
            try:
                self.remove(force=True)
            except NoSuchPlugin:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "plugin", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> PluginInspectResult:
        return PluginInspectResult.model_validate(json_object)

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def name(self) -> str | None:
        return self._get_inspect_result().name

    @property
    def enabled(self) -> bool | None:
        return self._get_inspect_result().enabled

    @property
    def settings(self) -> PluginSettings | None:
        return self._get_inspect_result().settings

    @property
    def plugin_reference(self) -> str | None:
        return self._get_inspect_result().plugin_reference

    @property
    def config(self) -> PluginConfig | None:
        return self._get_inspect_result().config

    @property
    def _docker_plugin(self) -> PluginCLI:
        return PluginCLI(self.client_config)

    @override
    def __repr__(self) -> str:
        return f"pywhales.Plugin(id='{self.id[:12]}', name='{self.name}')"

    def disable(self, force: bool = False) -> None:
        """Disable this plugin, optionally forcing an active plugin to stop."""
        self._docker_plugin.disable(self, force=force)

    def enable(self, timeout: int | None = None) -> None:
        """Enable this plugin with Docker's optional HTTP timeout in seconds.

        None omits the CLI timeout option; this is not a local process deadline.
        """
        self._docker_plugin.enable(self, timeout=timeout)

    def push(
        self, disable_content_trust: bool = True, *, timeout: float | None = None
    ) -> None:
        """Push this plugin to a registry.

        Args:
            disable_content_trust: No-op kept for compatibility; `False` raises
                `ValueError` (see `PluginCLI.push`).
            timeout: Positive finite seconds from the push CLI's own launch,
                replacing the client default; `None` inherits it.
        """
        self._docker_plugin.push(
            self, disable_content_trust=disable_content_trust, timeout=timeout
        )

    def remove(self, force: bool = False) -> None:
        """Remove this plugin, optionally forcing removal of an active plugin."""
        self._docker_plugin.remove(self, force=force)

    def exists(self) -> bool:
        """Return whether this plugin can still be inspected on the daemon."""
        return self._docker_plugin.exists(self.id)

    def set(self, configuration: dict[str, str]) -> None:
        """Set this plugin's configuration from key-value settings."""
        self._docker_plugin.set(self, configuration)

    def upgrade(
        self,
        remote: str | None = None,
        disable_content_trust: bool = True,
        skip_remote_check: bool = False,
        *,
        timeout: float | None = None,
    ) -> None:
        """Upgrade this plugin while automatically granting all permissions.

        See PluginCLI.upgrade for remote-selection, the content-trust no-op, and
        local `timeout` deadline options.
        """
        self._docker_plugin.upgrade(
            self, remote, disable_content_trust, skip_remote_check, timeout=timeout
        )


ValidPlugin = Plugin | str


class PluginCLI(DockerCLICaller):
    """Daemon-plugin commands, separate from local Buildx and Compose plugins."""

    def create(
        self, plugin_name: str, plugin_data_directory: ValidPath, compress: bool = False
    ) -> Plugin:
        """Create a plugin from a rootfs and configuration.

        Args:
            plugin_name: The name you want to give to your plugin
            plugin_data_directory: Must contain config.json and rootfs directory.
            compress: Compress the context using gzip

        Returns:
            An inspected handle to the created plugin.
        """
        full_cmd = Command([*self.docker_cmd, "plugin", "create"])
        full_cmd.add_flag("--compress", compress)
        full_cmd += [plugin_name, plugin_data_directory]
        run(full_cmd)
        return Plugin(self.client_config, plugin_name)

    def disable(self, plugin: ValidPlugin, force: bool = False) -> None:
        """Disable a plugin.

        Args:
            plugin: The plugin to disable
            force: Force the disable of an active plugin
        """
        full_cmd = Command([*self.docker_cmd, "plugin", "disable"])
        full_cmd.add_flag("--force", force)
        full_cmd.append(plugin)
        run(full_cmd)

    def enable(self, plugin: ValidPlugin, timeout: int | None = None) -> None:
        """Enable a plugin.

        Args:
            plugin: The plugin to enable
            timeout: Docker's HTTP client timeout in seconds. None omits the option
                and uses Docker's default, not a local subprocess deadline.
        """
        full_cmd = Command([*self.docker_cmd, "plugin", "enable"])
        full_cmd.add_simple_arg("--timeout", timeout)
        full_cmd.append(plugin)
        run(full_cmd)

    def exists(self, x: str) -> bool:
        """Return whether a plugin can be inspected.

        Args:
            x: Plugin name or ID to inspect.

        Returns:
            False when Docker reports NoSuchPlugin; True on successful inspection.
            Other inspection failures propagate.
        """
        try:
            self.inspect(x)
        except NoSuchPlugin:
            return False
        else:
            return True

    @overload
    def inspect(self, x: str) -> Plugin: ...

    @overload
    def inspect(self, x: builtins.list[str]) -> builtins.list[Plugin]: ...

    def inspect(self, x: str | builtins.list[str]) -> Plugin | builtins.list[Plugin]:
        """Inspect one or more plugins by name or ID.

        Args:
            x: One name or ID, or a list of references. An empty list does not
                invoke the CLI.

        Returns:
            One handle for a string, otherwise handles in request order. Batch
            inspection preserves duplicates as distinct handles and validates all
            snapshots before hydration. Full IDs must match exactly; aliases use
            positional association.

        Raises:
            ParsingError: If batch shape, plugin identities, or duplicate snapshot
                consistency is invalid.
        """
        if isinstance(x, str):
            return Plugin(self.client_config, x)
        else:
            if not x:
                return []
            parser = Plugin(self.client_config, x[0], is_immutable_id=True)
            snapshots = parse_namespace_inspect_results(
                parser, run([*self.docker_cmd, "plugin", "inspect", *x]), x
            )
            plugins = []
            for identity, snapshot in snapshots:
                plugin = Plugin(self.client_config, identity, is_immutable_id=True)
                plugin._set_inspect_result(snapshot)
                plugins.append(plugin)
            return plugins

    def install(
        self,
        plugin_name: str,
        configuration: Mapping[str, str] = MappingProxyType({}),
        alias: str | None = None,
        disable: bool = False,
        disable_content_trust: bool = True,
        *,
        timeout: float | None = None,
    ) -> Plugin:
        """Install a Docker plugin and automatically grant all permissions.

        Warning: `--grant-all-permissions` is enabled, which means the program won't
        stop to ask you to grant the permissions.

        Args:
            plugin_name: The name of the plugin you want to install
            configuration: Plugin-specific configuration names and values.
            alias: Local plugin name, or None to keep the requested name.
            disable: Do not enable the plugin on install
            disable_content_trust: No-op kept for compatibility. Docker CLI 29
                has no content-trust option for plugins, so `True` (the default)
                emits nothing and `False` is rejected.
            timeout: Positive finite seconds from the install CLI's own launch,
                replacing the client default; None inherits it. This is a local
                subprocess deadline, not Docker's own option.

        Returns:
            An inspected handle using the alias when supplied, otherwise the
            requested plugin name.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds, or if `disable_content_trust` is false.
                Raised before the Docker executable is looked up.
            DockerTimeout: If the install CLI exceeds its deadline. It is killed and
                reaped, without undoing the install or guaranteeing daemon-side
                work stops.
        """
        validate_timeout(timeout)
        _require_content_trust_skipped(disable_content_trust)
        full_cmd = Command(
            [*self.docker_cmd, "plugin", "install", "--grant-all-permissions"]
        )
        full_cmd.add_simple_arg("--alias", alias)
        full_cmd.add_flag("--disable", disable)
        full_cmd.append(plugin_name)
        for key, value in configuration.items():
            full_cmd.append(f"{key}={value}")
        run(full_cmd, timeout=timeout)
        if alias is not None:
            return Plugin(self.client_config, alias)
        return Plugin(self.client_config, plugin_name)

    def list(
        self, filters: Mapping[str, str] | Iterable[PluginListFilter] | None = None
    ) -> builtins.list[Plugin]:
        """Return handles for plugins installed on the selected daemon.

        Args:
            filters: Docker filter names and values, either as a mapping or as
                an iterable of ``(key, value)`` pairs, for example
                `filters=[("enabled", "true")]`. Pairs keep their order and
                repeated keys, and an iterator is read once. The documented
                keys are `capability` and `enabled`. None selects all plugins.

        Returns:
            Plugin handles in CLI output order.

        Raises:
            TypeError: If filters is a string, bytes, or an iterable containing
                anything but ``(key, value)`` tuples of strings. Raised before
                the Docker executable is looked up.
        """
        filter_args = [] if filters is None else format_filters_for_cli(filters)
        full_cmd = Command(
            [
                *self.docker_cmd,
                "plugin",
                "list",
                "--no-trunc",
                "--quiet",
            ]
        )
        full_cmd.add_args_iterable("--filter", filter_args)
        ids = run(full_cmd).splitlines()
        return [Plugin(self.client_config, id_, is_immutable_id=True) for id_ in ids]

    def push(
        self,
        plugin: ValidPlugin,
        disable_content_trust: bool = True,
        *,
        timeout: float | None = None,
    ) -> None:
        """Push a plugin to a registry.

        Args:
            plugin: The plugin to push
            disable_content_trust: No-op kept for compatibility. Docker CLI 29
                has no content-trust option for plugins, so `True` (the default)
                emits nothing and `False` is rejected.
            timeout: Positive finite seconds from the push CLI's own launch,
                replacing the client default; `None` inherits it. This is a
                local subprocess deadline, not the daemon-side HTTP timeout
                used by `enable()`.

        Raises:
            ValueError: `timeout` is not positive and finite, or
                `disable_content_trust` is false. Raised before the Docker
                executable is looked up.
            DockerTimeout: The push CLI exceeds its deadline and is killed and
                reaped, without undoing the push or guaranteeing that
                daemon-side work stops.
        """
        validate_timeout(timeout)
        _require_content_trust_skipped(disable_content_trust)
        full_cmd = Command([*self.docker_cmd, "plugin", "push"])
        full_cmd.append(plugin)
        run(full_cmd, timeout=timeout)

    def remove(
        self, x: ValidPlugin | Iterable[ValidPlugin], force: bool = False
    ) -> None:
        """Remove one or more plugins.

        Args:
            x: Plugin names, IDs, or handles. An empty selection is a no-op.
            force: Force the removal of this plugin.
        """
        plugins = to_list(x)
        if not plugins:
            return
        full_cmd = Command([*self.docker_cmd, "plugin", "remove"])
        full_cmd.add_flag("--force", force)
        full_cmd += plugins
        run(full_cmd)

    def set(self, plugin: ValidPlugin, configuration: dict[str, str]) -> None:
        """Change the settings for a plugin.

        Args:
            plugin: The plugin that needs its settings changed
            configuration: The new configuration options.
        """
        full_cmd = Command([*self.docker_cmd, "plugin", "set"])
        full_cmd.append(plugin)
        for key, value in configuration.items():
            full_cmd.append(f"{key}={value}")
        run(full_cmd)

    def upgrade(
        self,
        plugin: ValidPlugin,
        remote: str | None = None,
        disable_content_trust: bool = True,
        skip_remote_check: bool = False,
        *,
        timeout: float | None = None,
    ) -> None:
        """Upgrade a plugin while automatically granting all permissions.

        Warning: `--grant-all-permissions` is enabled, which means the program won't
        stop to ask you to grant the permissions.

        Args:
            plugin: The plugin to upgrade
            remote: Remote plugin reference, or None to omit it.
            disable_content_trust: No-op kept for compatibility. Docker CLI 29
                has no content-trust option for plugins, so `True` (the default)
                emits nothing and `False` is rejected.
            skip_remote_check: Do not check if specified remote plugin matches
                existing plugin image
            timeout: Positive finite seconds from the upgrade CLI's own launch,
                replacing the client default; None inherits it. This is a local
                subprocess deadline, not Docker's own option.

        Raises:
            ValueError: If timeout is a Boolean or is neither None nor a positive
                finite number of seconds, or if `disable_content_trust` is false.
                Raised before the Docker executable is looked up.
            DockerTimeout: If the CLI exceeds its deadline. It is killed and reaped,
                without undoing the upgrade or guaranteeing daemon-side work stops.
        """
        validate_timeout(timeout)
        _require_content_trust_skipped(disable_content_trust)
        full_cmd = Command(
            [*self.docker_cmd, "plugin", "upgrade", "--grant-all-permissions"]
        )
        full_cmd.add_flag("--skip-remote-check", skip_remote_check)
        full_cmd.append(plugin)
        if remote is not None:
            full_cmd.append(remote)
        run(full_cmd, timeout=timeout)
