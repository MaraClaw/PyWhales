"""Validate and encode network creation options before executable lookup."""

from collections.abc import Mapping, Sequence
from ipaddress import (
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_interface,
    ip_network,
)
from typing import Literal

from pywhales.components.network.models import NetworkIPAMPool

_SCOPES = ("local", "swarm")


def _mapping_entries(
    mapping: Mapping[str, str | int], *, strings_only: bool = False
) -> list[str]:
    if not isinstance(mapping, Mapping):
        raise TypeError("IPAM options and auxiliary addresses must be mappings")
    entries = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not key or "=" in key:
            raise ValueError("IPAM mapping keys must be nonempty strings without '='")
        if not isinstance(value, str) and (strings_only or type(value) is not int):
            raise TypeError("IPAM mapping values must be strings or integers")
        entries.append(f"{key}={value}")
    return entries


def _single_csv_value(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or any(char in value for char in ',"\r\n')
    ):
        raise ValueError("Network address options must be single CSV values")


def _check_associations(pools: Sequence[NetworkIPAMPool]) -> None:
    if any(
        "/" not in pool.subnet
        or not pool.subnet.rsplit("/", 1)[1].isascii()
        or not pool.subnet.rsplit("/", 1)[1].isdecimal()
        or "%" in pool.subnet
        for pool in pools
    ):
        return
    try:
        networks = [ip_network(pool.subnet, strict=False) for pool in pools]
    except ValueError:
        return
    for index, network in enumerate(networks):
        if isinstance(network, IPv6Network):
            mapped = network.network_address.ipv4_mapped
            if mapped is not None:
                networks[index] = IPv4Network((mapped, network.prefixlen - 96))
    for index, network in enumerate(networks):
        if any(
            network.version == other.version and network.overlaps(other)
            for other in networks[index + 1 :]
        ):
            return
    associations: list[tuple[int, int, str]] = []
    for index, pool in enumerate(pools):
        values = [(pool.gateway, "gateway"), (pool.ip_range, "range")]
        values.extend((address, "aux") for address in pool.auxiliary_addresses.values())
        for value, kind in values:
            if value is None:
                continue
            if "%" in value or (
                "/" in value
                and (
                    not value.rsplit("/", 1)[1].isascii()
                    or not value.rsplit("/", 1)[1].isdecimal()
                )
            ):
                return
            try:
                if kind == "range" and "/" not in value:
                    return
                address = ip_interface(value).ip if "/" in value else ip_address(value)
            except ValueError:
                return
            if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            matches = [
                position
                for position, network in enumerate(networks)
                if address.version == network.version and address in network
            ]
            if not matches:
                return
            associations.append((index, matches[0], kind))
    for kind in ("gateway", "range"):
        destinations = [
            target for _, target, entry_kind in associations if entry_kind == kind
        ]
        if len(destinations) != len(set(destinations)):
            return
    if any(source != target for source, target, _ in associations):
        raise ValueError("IPAM address would be associated with a different pool")


def create_options(
    *,
    gateway: str | None,
    subnet: str | None,
    internal: bool | None,
    ipv4: bool | None,
    enable_ipv6: bool | None,
    scope: Literal["local", "swarm"] | None,
    ipam_driver: str | None,
    ipam_options: Mapping[str, str | int] | None,
    ipam_pools: Sequence[NetworkIPAMPool] | None,
) -> list[str]:
    """Return validated option arguments without consulting the Docker executable."""
    if ipam_pools is not None and (gateway is not None or subnet is not None):
        raise ValueError("ipam_pools cannot be combined with subnet or gateway")
    if scope is not None and scope not in _SCOPES:
        raise ValueError("scope must be 'local' or 'swarm'")
    result = []
    for flag, value in (
        ("--internal", internal),
        ("--ipv4", ipv4),
        ("--ipv6", enable_ipv6),
    ):
        if value is not None and type(value) is not bool:
            raise TypeError("network boolean options must be bool or None")
        if value is True:
            result.append(flag)
        if value is False:
            result.append(f"{flag}=false")
    if scope is not None:
        result.extend(["--scope", scope])
    if ipam_driver is not None:
        if not isinstance(ipam_driver, str):
            raise TypeError("ipam_driver must be a string or None")
        result.extend(["--ipam-driver", ipam_driver])
    if ipam_options is not None:
        for entry in _mapping_entries(ipam_options):
            result.extend(["--ipam-opt", entry])
    if ipam_pools is None:
        return result
    if not isinstance(ipam_pools, Sequence) or isinstance(ipam_pools, (str, bytes)):
        raise TypeError("ipam_pools must be a sequence of NetworkIPAMPool")
    pools = []
    names: set[str] = set()
    for pool in ipam_pools:
        if not isinstance(pool, NetworkIPAMPool):
            raise TypeError("ipam_pools must contain NetworkIPAMPool values")
        pool = NetworkIPAMPool.model_validate(pool.model_dump())
        for value in (pool.subnet, pool.gateway, pool.ip_range):
            _single_csv_value(value)
        entries = _mapping_entries(pool.auxiliary_addresses, strings_only=True)
        if names.intersection(pool.auxiliary_addresses):
            raise ValueError("auxiliary address names must be unique across pools")
        names.update(pool.auxiliary_addresses)
        pools.append(pool)
        result.extend(["--subnet", pool.subnet])
        if pool.gateway is not None:
            result.extend(["--gateway", pool.gateway])
        if pool.ip_range is not None:
            result.extend(["--ip-range", pool.ip_range])
        for entry in entries:
            result.extend(["--aux-address", entry])
    _check_associations(pools)
    return result
