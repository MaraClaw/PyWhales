"""Network handles and Docker CLI operations against the selected daemon."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType, TracebackType
from typing import (
    Literal,
    Self,
    overload,
    override,
)

from pydantic import JsonValue

import pywhales.components.container.cli_wrapper
from pywhales.client_config import (
    ClientConfig,
    Command,
    DockerCLICaller,
    ParsingError,
    ReloadableObjectFromJson,
)
from pywhales.components._cleanup import resource_cleanup
from pywhales.components.network._create_options import create_options
from pywhales.components.network.models import (
    NetworkContainer,
    NetworkInspectResult,
    NetworkIPAM,
    NetworkIPAMPool,
)
from pywhales.enums import ResourceScope, ResourceScopeInput
from pywhales.exceptions import NoSuchNetwork
from pywhales.utils import format_mapping_for_cli, run, to_list, validate_timeout

type NetworkListFilter = (
    tuple[Literal["driver"], str]
    | tuple[Literal["id"], str]
    | tuple[Literal["label"], str]
    | tuple[Literal["label!"], str]
    | tuple[Literal["name"], str]
    | tuple[Literal["scope"], ResourceScopeInput]
    | tuple[Literal["type"], Literal["custom", "builtin"]]
    | tuple[Literal["until"], str]
    | tuple[Literal["dangling"], str]
)


class Network(ReloadableObjectFromJson[NetworkInspectResult]):
    """Reloadable network handle retaining its originating client settings.

    Context-manager exit attempts removal, ignoring an already absent network.
    Other cleanup failures are grouped with any active body exception.
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
        with resource_cleanup(exc_val, "Network body and cleanup both failed"):
            try:
                self.remove()
            except NoSuchNetwork:
                return

    @override
    def _fetch_inspect_result_json(self, reference: str) -> dict[str, JsonValue]:
        json_str = run([*self.docker_cmd, "network", "inspect", reference])
        return self._parse_single_inspect_json(json_str)

    @override
    def _parse_json_object(
        self, json_object: dict[str, JsonValue]
    ) -> NetworkInspectResult:
        return NetworkInspectResult.model_validate(json_object)

    @property
    def name(self) -> str | None:
        return self._get_inspect_result().name

    @property
    def id(self) -> str:
        return self._get_immutable_id()

    @property
    def created(self) -> datetime | None:
        return self._get_inspect_result().created

    @property
    def scope(self) -> ResourceScope | str | None:
        return self._get_inspect_result().scope

    @property
    def driver(self) -> str | None:
        return self._get_inspect_result().driver

    @property
    def enable_ipv6(self) -> bool | None:
        return self._get_inspect_result().enable_ipv6

    @property
    def ipam(self) -> NetworkIPAM | None:
        return self._get_inspect_result().ipam

    @property
    def internal(self) -> bool | None:
        return self._get_inspect_result().internal

    @property
    def attachable(self) -> bool | None:
        return self._get_inspect_result().attachable

    @property
    def ingress(self) -> bool | None:
        return self._get_inspect_result().ingress

    @property
    def containers(self) -> dict[str, NetworkContainer] | None:
        return self._get_inspect_result().containers

    @property
    def options(self) -> dict[str, JsonValue] | None:
        return self._get_inspect_result().options

    @property
    def labels(self) -> dict[str, str] | None:
        return self._get_inspect_result().labels

    @property
    def config_from(self) -> dict[str, JsonValue] | None:
        return self._get_inspect_result().config_from

    @property
    def config_only(self) -> bool | None:
        return self._get_inspect_result().config_only

    @override
    def __repr__(self) -> str:
        return f"pywhales.Network(id='{self.id[:12]}', name={self.name})"

    def exists(self) -> bool:
        """Return whether this network can still be inspected."""
        return NetworkCLI(self.client_config).exists(self.id)

    def remove(self) -> None:
        """Remove this Docker network.

        A context manager attempts this removal even if its body raises.
        """
        NetworkCLI(self.client_config).remove(self)


ValidNetwork = Network | str


class NetworkCLI(DockerCLICaller):
    """Daemon network commands retaining the originating client configuration."""

    def connect(
        self,
        network: ValidNetwork,
        container: pywhales.components.container.cli_wrapper.ValidContainer,
        alias: str | None = None,
        driver_options: Sequence[str] = (),
        ip: str | None = None,
        ip6: str | None = None,
        links: Sequence[pywhales.components.container.cli_wrapper.ValidContainer] = (),
    ) -> None:
        """Connect a container to a network.

        Args:
            network: The network to connect to
            container: The container to connect
            alias: Network-scoped alias, or None to omit it.
            driver_options: Driver options for the network
            ip: IPv4 address, or None to let Docker choose.
            ip6: IPv6 address, or None to let Docker choose.
            links: Add links to other containers
        """
        full_cmd = Command([*self.docker_cmd, "network", "connect"])
        full_cmd.add_simple_arg("--alias", alias)
        full_cmd.add_args_iterable_or_single("--driver-opt", driver_options)
        full_cmd.add_simple_arg("--ip", ip)
        full_cmd.add_simple_arg("--ip6", ip6)
        full_cmd.add_args_iterable_or_single("--link", links)
        full_cmd += [network, container]
        run(full_cmd)

    def create(
        self,
        name: str,
        attachable: bool = False,
        driver: str | None = None,
        gateway: str | None = None,
        subnet: str | None = None,
        labels: Mapping[str, str] = MappingProxyType({}),
        options: Sequence[str] = (),
        *,
        internal: bool | None = None,
        enable_ipv6: bool | None = None,
        ipam_driver: str | None = None,
        ipam_options: Mapping[str, str | int] | None = None,
        ipam_pools: Sequence[NetworkIPAMPool] | None = None,
    ) -> Network:
        """Create a Docker network.

        Args:
            name: Name for the network.
            attachable: Allow manually started containers to attach.
            driver: Network driver, or None to use Docker's default.
            gateway: Gateway address, or None to let Docker choose.
            subnet: Subnet in CIDR notation, or None to let Docker choose.
            labels: Metadata labels; the default adds none.
            options: Driver-specific key=value options; the default adds none.
            internal: Set internal mode explicitly, or omit when None.
            enable_ipv6: Set IPv6 explicitly, or omit when None.
            ipam_driver: IPAM driver, or None for Docker's default.
            ipam_options: IPAM driver options; None adds none.
            ipam_pools: Ordered pools; cannot be combined with gateway or subnet.

        Returns:
            A handle to the created network without an initial inspect call.

        Raises:
            ValueError: For conflicting or ambiguous pool settings, invalid
                mapping keys, or pool address values that expand as CSV.
            TypeError: For malformed option structures or boolean values.
        """
        extra_options = create_options(
            gateway=gateway,
            subnet=subnet,
            internal=internal,
            enable_ipv6=enable_ipv6,
            ipam_driver=ipam_driver,
            ipam_options=ipam_options,
            ipam_pools=ipam_pools,
        )
        full_cmd = Command([*self.docker_cmd, "network", "create"])
        full_cmd.add_flag("--attachable", attachable)
        full_cmd.add_simple_arg("--driver", driver)
        full_cmd.add_simple_arg("--gateway", gateway)
        full_cmd.add_simple_arg("--subnet", subnet)
        full_cmd.add_args_iterable_or_single("--label", format_mapping_for_cli(labels))
        full_cmd.add_args_iterable_or_single("--opt", options)
        full_cmd.extend(extra_options)
        full_cmd.append(name)
        return Network(self.client_config, run(full_cmd), is_immutable_id=True)

    def disconnect(
        self,
        network: ValidNetwork,
        container: pywhales.components.container.cli_wrapper.ValidContainer,
        force: bool = False,
    ) -> None:
        """Disconnect a container from a network.

        Args:
            network: The name of the network.
            container: The container to disconnect.
            force: Force Docker to disconnect the container when True.
        """
        full_cmd = Command([*self.docker_cmd, "network", "disconnect"])
        full_cmd.add_flag("--force", force)
        full_cmd += [network, container]
        run(full_cmd)

    def exists(
        self,
        network: ValidNetwork,
    ) -> bool:
        """Return whether a network can be inspected.

        Args:
            network: Network name, ID, or handle to inspect.

        Returns:
            False when Docker reports NoSuchNetwork; True on successful inspection.
            Other inspection failures propagate.
        """
        try:
            self.inspect(str(network))
        except NoSuchNetwork:
            return False
        else:
            return True

    @overload
    def inspect(self, x: str) -> Network: ...

    @overload
    def inspect(self, x: Iterable[str]) -> builtins.list[Network]: ...

    def inspect(self, x: str | Iterable[str]) -> Network | builtins.list[Network]:
        """Inspect one or more networks by name or ID.

        Args:
            x: One name or ID, or an iterable of references. An empty iterable
                returns without invoking the CLI.

        Returns:
            One handle for a string, otherwise handles in request order with
            duplicates retained as distinct handles.

        Raises:
            ParsingError: If a batch response has invalid shape, inconsistent
                duplicate snapshots, or identities that do not match the requests.
        """
        if isinstance(x, str):
            return Network(self.client_config, x)
        references = list(x)
        if not references:
            return []
        json_str = run([*self.docker_cmd, "network", "inspect", *references])
        parser = Network(self.client_config, references[0], is_immutable_id=True)
        snapshots = parser._parse_inspect_results(json_str, references)
        networks = []
        for reference, snapshot in zip(references, snapshots, strict=True):
            if snapshot.id is None or not (
                reference
                and (snapshot.id.startswith(reference) or snapshot.name == reference)
            ):
                raise ParsingError(
                    f"Network inspect identity does not match {reference!r}"
                    if self.client_config.raw_diagnostics
                    else "Network inspect identity does not match the requested reference"
                )
            network = Network(self.client_config, snapshot.id, is_immutable_id=True)
            network._set_inspect_result(snapshot)
            networks.append(network)
        return networks

    def list(self, filters: Iterable[NetworkListFilter] = ()) -> builtins.list[Network]:
        """List all the networks available.

        Args:
            filters: Docker filter pairs; the default selects all networks.

        Returns:
            Network handles in CLI output order.
        """
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "network", "ls", "--no-trunc", "--quiet"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )

        ids = run(full_cmd).splitlines()
        return [Network(self.client_config, id_, is_immutable_id=True) for id_ in ids]

    def prune(
        self, filters: Iterable[NetworkListFilter] = (), *, timeout: float | None = None
    ) -> None:
        """Remove Docker networks which are not used by any containers.

        Docker's confirmation prompt is bypassed.

        Args:
            filters: Docker filter pairs restricting removal; the default adds no filters.
            timeout: Positive finite seconds from local CLI launch; `None` inherits
                the client default. Expiry cannot undo daemon-side removal.
        """
        validate_timeout(timeout)
        if isinstance(filters, Mapping):
            raise TypeError("filters must be an iterable of pairs, not a mapping")
        full_cmd = Command([*self.docker_cmd, "network", "prune", "--force"])
        full_cmd.add_args_iterable(
            "--filter", (f"{key}={value}" for key, value in filters)
        )
        run(full_cmd, timeout=timeout)

    def remove(self, networks: ValidNetwork | Iterable[ValidNetwork]) -> None:
        """Remove one or more Docker networks.

        Args:
            networks: Network names, IDs, or handles. An empty selection is a no-op.
        """
        selected = to_list(networks)
        if not selected:
            return
        full_cmd = Command([*self.docker_cmd, "network", "remove"])
        full_cmd += selected
        run(full_cmd)
