"""GPU capability of the selected Docker daemon, derived from its reported info.

Everything here describes the daemon the client is configured for (its context,
host or SSH endpoint), never the machine running Python. Nothing inspects local
devices or drivers. The module also holds the pure helpers that turn `--gpus`
values, Compose configuration and Swarm generic resource requests into
comparisons against what the daemon or its nodes report.
"""

import csv
import io
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from pywhales.components.system.models import SystemInfo
from pywhales.exceptions import GpuSupportError

if TYPE_CHECKING:
    from pywhales.components.node.cli_wrapper import Node

GPU_DEVICE_KINDS: frozenset[str] = frozenset({"nvidia.com/gpu", "amd.com/gpu"})
"""CDI device kinds that Docker's `--gpus` option can inject."""

GPU_RUNTIMES: frozenset[str] = frozenset({"nvidia"})
"""Registered runtime names that indicate an installed GPU container toolkit."""

_CDI_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_-]*=[A-Za-z0-9][A-Za-z0-9._:-]*"
)
_NVIDIA_GPU_KIND = "nvidia.com/gpu"
_DECIMAL = re.compile(r"[0-9]+")
_GENERIC_AMOUNT = re.compile(r"[+]?[0-9]+")
_SIGNED_INT = re.compile(r"[+-]?[0-9]+")
_MIG_INDEX = re.compile(r"[0-9]+:[0-9]+")
_VERIFIED_FAMILIES = frozenset({"index", "uuid"})
# Capabilities that moby's NVIDIA driver (gpu, nvidia, compute, compat32, graphics,
# utility, video, display) or AMD driver (gpu, amd) accept; a Compose device
# reservation that names one asks for a GPU.
_GPU_CAPABILITIES = frozenset(
    {
        "gpu",
        "nvidia",
        "compute",
        "compat32",
        "graphics",
        "utility",
        "video",
        "display",
        "amd",
    }
)


def cdi_device_kind(device: str) -> str | None:
    """Return the ``vendor/class`` kind of a fully qualified CDI device name.

    Args:
        device: A `--device` value, such as ``"nvidia.com/gpu=0"`` or a host
            mapping like ``"/dev/fuse"``.

    Returns:
        The kind, for example ``"nvidia.com/gpu"``, or `None` when the value
        isn't a qualified CDI name.
    """
    if _CDI_NAME.fullmatch(device) is None:
        return None
    return device.partition("=")[0]


def _device_value(device: str) -> str:
    """Return the part of a CDI name after ``=``."""
    return device.partition("=")[2]


def _parse_count(text: str) -> int | None:
    """Return the count a `--gpus` field denotes, or `None` when the CLI rejects it."""
    if text == "all":
        return -1
    if _SIGNED_INT.fullmatch(text) is None:
        return None
    return int(text)


def _name_family(name: str) -> tuple[str, str] | None:
    """Return the kind and naming family of a GPU device name.

    Args:
        name: A fully qualified CDI name.

    Returns:
        The kind with the family: ``"index"`` for a whole-GPU index such as ``0``,
        ``"mig_index"`` for ``0:1``, ``"uuid"`` for ``GPU-`` values and
        ``"mig_uuid"`` for ``MIG-`` values; `None` when the kind isn't in
        `GPU_DEVICE_KINDS` or the value matches no family.
    """
    kind = cdi_device_kind(name)
    if kind not in GPU_DEVICE_KINDS:
        return None
    value = _device_value(name)
    if _DECIMAL.fullmatch(value) is not None:
        return kind, "index"
    if _MIG_INDEX.fullmatch(value) is not None:
        return kind, "mig_index"
    if value.startswith("GPU-"):
        return kind, "uuid"
    if value.startswith("MIG-"):
        return kind, "mig_uuid"
    return None


def _resolved_names(request: GpuRequest, index_limit: int) -> Iterator[str]:
    """Yield the CDI names Docker's NVIDIA driver derives from a request.

    A count of N yields indexes ``0`` through ``N-1``, not the first N devices
    that exist, stopping at `index_limit` so a huge count stays bounded. Counts
    of zero or less, including all GPUs, yield nothing. Device IDs without ``=``
    get the ``nvidia.com/gpu`` kind.

    Args:
        request: The GPU request to resolve.
        index_limit: Exclusive upper bound on the count indexes yielded.
    """
    if request.count is not None:
        for index in range(min(request.count, index_limit)):
            yield f"{_NVIDIA_GPU_KIND}={index}"
    for device_id in request.device_ids:
        yield device_id if "=" in device_id else f"{_NVIDIA_GPU_KIND}={device_id}"


@dataclass(frozen=True, slots=True)
class GpuRequest:
    """One GPU request in the shape `--gpus` and Compose express it.

    Attributes:
        count: Number of GPUs wanted, ``-1`` for all of them, or `None` when the
            request doesn't give a count that can be verified.
        device_ids: Device selection. Docker's NVIDIA driver reads bare values as
            indexes or UUIDs of ``nvidia.com/gpu`` and passes names containing
            ``=`` through.
        driver: Requested device driver, or `None` when unspecified.
    """

    count: int | None = None
    device_ids: tuple[str, ...] = ()
    driver: str | None = None

    @classmethod
    def parse(cls, gpus: int | str) -> GpuRequest | None:
        """Parse a `--gpus` value the way the Docker CLI does.

        A string is one CSV record. A field without ``=`` is a count (``all`` or an
        integer); ``count=``, ``device=``, ``driver=``, ``capabilities=`` and
        ``options=`` are the keys. As in the CLI, a key repeats only by its field text,
        and a bare count also claims ``count``: ``"2,count=3"`` and ``"2,2"`` are
        rejected, while ``"2,3"`` keeps the last count. ``device`` splits on commas, so callers quote it
        as ``'"device=0,1"'``. Without a count or a device selection the count is
        1. ``capabilities`` and ``options`` are accepted and ignored.

        Args:
            gpus: The `--gpus` value; an integer is a plain count.

        Returns:
            The parsed request, or `None` for a value the CLI would reject: an
            empty or malformed record, a bad count, or an unknown or repeated key.
            The CLI reports those errors itself.
        """
        if isinstance(gpus, int):
            return cls(count=gpus)
        try:
            fields = next(csv.reader(io.StringIO(gpus), strict=True), None)
        except csv.Error:
            return None
        if fields is None or not fields:
            return None
        count: int | None = None
        device_ids: tuple[str, ...] = ()
        driver: str | None = None
        seen: set[str] = set()
        for field in fields:
            option, separator, text = field.partition("=")
            if option in seen:
                return None
            seen.add(option)
            key, value = (option, text) if separator else ("count", option)
            if not separator:
                seen.add("count")
            match key:
                case "count":
                    count = _parse_count(value)
                    if count is None:
                        return None
                case "device":
                    device_ids = tuple(value.split(","))
                case "driver":
                    driver = value
                case "capabilities" | "options":
                    pass
                case _:
                    return None
        if count is None and not device_ids:
            count = 1
        return cls(count=count, device_ids=device_ids, driver=driver)


@dataclass(frozen=True, slots=True)
class GpuSupport:
    """GPU evidence reported by one Docker daemon.

    A daemon can serve `--gpus` through a registered NVIDIA runtime (the legacy
    toolkit hook) or through CDI devices of a known GPU kind (Docker 29). NVIDIA
    vGPU guests and MIG partitions appear as ordinary ``nvidia.com/gpu`` CDI
    devices. A host whose only GPU path is the toolkit hook, with no registered
    runtime and no CDI specification, reports no evidence here.

    Attributes:
        os_type: The daemon's container OS, such as ``"linux"``.
        gpu_runtimes: Registered runtimes named in `GPU_RUNTIMES`, sorted.
        cdi_spec_dirs: Directories the daemon reads CDI specifications from;
            empty when CDI is disabled or not reported.
        cdi_devices: CDI device names the daemon discovered, in reported order,
            or `None` when the daemon predates the list (Docker before 28).
    """

    os_type: str | None
    gpu_runtimes: tuple[str, ...]
    cdi_spec_dirs: tuple[str, ...]
    cdi_devices: tuple[str, ...] | None

    @classmethod
    def from_info(cls, info: SystemInfo) -> GpuSupport:
        """Extract GPU evidence from parsed `docker system info` output.

        Args:
            info: The daemon's system information.

        Returns:
            The daemon's GPU evidence.
        """
        devices = None
        if info.discovered_devices is not None:
            devices = tuple(
                device.id
                for device in info.discovered_devices
                if device.source == "cdi" and device.id is not None
            )
        return cls(
            os_type=info.os_type,
            gpu_runtimes=tuple(sorted(set(info.runtimes or {}) & GPU_RUNTIMES)),
            cdi_spec_dirs=tuple(info.cdi_spec_dirs or ()),
            cdi_devices=devices,
        )

    @property
    def gpu_devices(self) -> tuple[str, ...]:
        """Discovered CDI devices whose kind is in `GPU_DEVICE_KINDS`."""
        return tuple(
            device
            for device in self.cdi_devices or ()
            if cdi_device_kind(device) in GPU_DEVICE_KINDS
        )

    @property
    def whole_gpus(self) -> tuple[str, ...]:
        """Discovered GPU devices named by a bare index, such as ``nvidia.com/gpu=0``."""
        return tuple(
            device
            for device in self.gpu_devices
            if _DECIMAL.fullmatch(_device_value(device)) is not None
        )

    @property
    def gpu_uuids(self) -> tuple[str, ...]:
        """Discovered GPU devices named by a ``GPU-`` UUID."""
        return tuple(
            device
            for device in self.gpu_devices
            if _device_value(device).startswith("GPU-")
        )

    @property
    def mig_devices(self) -> tuple[str, ...]:
        """Discovered GPU devices naming a MIG partition by ``0:1`` index or ``MIG-`` UUID."""
        return tuple(
            device
            for device in self.gpu_devices
            if _MIG_INDEX.fullmatch(_device_value(device)) is not None
            or _device_value(device).startswith("MIG-")
        )

    @property
    def gpu_count(self) -> int | None:
        """Number of GPUs the daemon names by bare index, or `None` if it reports no list.

        A host whose CDI specification names no GPU by index reports 0, even when
        it names GPUs another way. The type-index aliases (``gpu0``, ``mig0:1``) and
        ``all`` are left unclassified and never counted.
        """
        if self.cdi_devices is None:
            return None
        return len(self.whole_gpus)

    @property
    def gpus_available(self) -> bool:
        """Whether the daemon runs Linux containers and reports a GPU path."""
        return self.os_type == "linux" and bool(self.gpu_runtimes or self.gpu_devices)

    def check(self, *, gpus: int | str | None, devices: Iterable[str] = ()) -> None:
        """Raise unless the daemon reports support for the given GPU requests.

        Parses `gpus` with `GpuRequest.parse` and delegates to `check_requests`,
        whose rules for counts, selections and MIG names apply. A value the parser
        rejects is checked as a default request, leaving the CLI to report the
        syntax error.

        Args:
            gpus: The `--gpus` value, or `None` when not requested.
            devices: `--device` values; only qualified CDI names are checked,
                and each must appear verbatim among `cdi_devices`.

        Raises:
            GpuSupportError: If `gpus` is requested without `gpus_available`, a
                requested CDI device isn't reported, or a count or device
                selection names GPUs the daemon doesn't report. A non-Linux
                daemon fails either request with reason ``"not_linux"``.
        """
        requests = [] if gpus is None else [GpuRequest.parse(gpus) or GpuRequest()]
        self.check_requests(requests, devices)

    def check_requests(
        self, requests: Iterable[GpuRequest], devices: Iterable[str] = ()
    ) -> None:
        """Raise unless the daemon reports support for the given GPU requests.

        Refusals are checked in a fixed order: a non-Linux daemon, then a request
        that needs a GPU driver the daemon doesn't report, then explicit CDI names
        that can't be verified or aren't reported, then GPU names a count or device
        selection resolves to.

        Explicit CDI names (`devices` and the device IDs of a ``cdi`` driver
        request) must be reported verbatim. A name that Docker's NVIDIA driver
        derives from a count or a device selection is checked more loosely: it is
        refused only when the daemon reports at least one name of the same kind and
        naming family (whole index or ``GPU-`` UUID) but not that name. MIG names,
        meaning ``0:1`` indexes and ``MIG-`` UUIDs, are never verified on this path,
        because MIG layouts change at runtime and Docker falls back to the runtime
        hook. That hook is also Docker's fallback after any CDI failure, since its
        NVIDIA driver tries CDI first. A ``"gpu_device_missing"`` refusal therefore
        assumes the host's CDI specification is current; regenerate it with
        ``nvidia-ctk cdi generate`` after changing GPUs. Requests with a driver other
        than ``nvidia`` or ``cdi`` aren't resolved.

        Args:
            requests: GPU requests to verify. Consumed once.
            devices: `--device` values; only qualified CDI names are checked,
                and each must appear verbatim among `cdi_devices`. Consumed once.

        Raises:
            GpuSupportError: With reason ``"not_linux"``, ``"no_gpu_driver"``,
                ``"cdi_devices_unreported"``, ``"cdi_device_missing"`` (the names
                in request order) or ``"gpu_device_missing"`` (the resolved names,
                deduplicated, in first-seen order, never a MIG name; a count lists
                its missing indexes up to the first one past the highest reported
                index).
        """
        wanted = tuple(requests)
        cdi_names = [
            device for device in devices if cdi_device_kind(device) is not None
        ]
        if not wanted and not cdi_names:
            return
        if self.os_type != "linux":
            raise GpuSupportError("not_linux")
        if not self.gpus_available and any(r.driver != "cdi" for r in wanted):
            raise GpuSupportError("no_gpu_driver")
        cdi_names.extend(
            name
            for request in wanted
            if request.driver == "cdi"
            for name in request.device_ids
        )
        if self.cdi_devices is None:
            if cdi_names:
                raise GpuSupportError("cdi_devices_unreported")
            return
        reported = set(self.cdi_devices)
        absent = tuple(name for name in cdi_names if name not in reported)
        if absent:
            raise GpuSupportError("cdi_device_missing", absent)
        # MIG families never enter this set, so MIG names pass unverified.
        families = {
            family
            for name in self.gpu_devices
            if (family := _name_family(name)) and family[1] in _VERIFIED_FAMILIES
        }
        # Every index past the highest reported one is missing; listing the first
        # of them is enough and keeps a huge count bounded.
        index_limit = 2 + max(
            (
                int(_device_value(name))
                for name in self.whole_gpus
                if cdi_device_kind(name) == _NVIDIA_GPU_KIND
            ),
            default=-2,
        )
        missing = dict.fromkeys(
            name
            for request in wanted
            if request.driver in (None, "", "nvidia")
            for name in _resolved_names(request, index_limit)
            if name not in reported and _name_family(name) in families
        )
        if missing:
            raise GpuSupportError("gpu_device_missing", tuple(missing))


def _compose_request(entry: JsonValue) -> GpuRequest | None:
    """Return the request a normalized Compose device entry describes.

    A missing count with no device IDs means all GPUs, Compose's default. Returns
    `None` for an entry whose shape or types don't match.
    """
    if not isinstance(entry, dict):
        return None
    count = entry.get("count")
    ids = entry.get("device_ids")
    driver = entry.get("driver")
    device_ids: tuple[str, ...] = ()
    if ids is not None:
        if not isinstance(ids, list):
            return None
        device_ids = tuple(item for item in ids if isinstance(item, str))
        if len(device_ids) != len(ids):
            return None
    if driver is not None and not isinstance(driver, str):
        return None
    if count is None:
        resolved = None if device_ids else -1
    elif count == "all":
        resolved = -1
    elif type(count) is int:
        resolved = count
    else:
        return None
    return GpuRequest(count=resolved, device_ids=device_ids, driver=driver)


def _capability_names(capabilities: JsonValue) -> Iterator[str]:
    """Yield the strings of a Compose capabilities list, flattening nested lists."""
    if isinstance(capabilities, list):
        for item in capabilities:
            if isinstance(item, str):
                yield item
            else:
                yield from _capability_names(item)


def _wants_gpu(entry: Mapping[str, JsonValue]) -> bool:
    """Whether a Compose reservation device asks for a GPU.

    It does when a capability is one of the NVIDIA or AMD GPU capabilities or the
    driver is ``nvidia`` or ``cdi``.
    """
    if not _GPU_CAPABILITIES.isdisjoint(_capability_names(entry.get("capabilities"))):
        return True
    return entry.get("driver") in ("nvidia", "cdi")


def _reserved_devices(service: Mapping[str, JsonValue]) -> list[JsonValue]:
    """Return `deploy.resources.reservations.devices`, or an empty list when absent."""
    node: JsonValue = dict(service)
    for key in ("deploy", "resources", "reservations", "devices"):
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


def _compose_device_name(item: JsonValue) -> str | None:
    """Return the CDI name a Compose `devices` item holds, or `None` for other devices.

    Like Compose itself, an item is a CDI device only when it is a mapping whose
    ``source`` and ``target`` are the same qualified CDI name. Normalized
    configuration always holds mappings.
    """
    if not isinstance(item, dict):
        return None
    source = item.get("source")
    if (
        isinstance(source, str)
        and source == item.get("target")
        and cdi_device_kind(source) is not None
    ):
        return source
    return None


def compose_gpu_requests(
    services: Mapping[str, JsonValue],
) -> tuple[tuple[GpuRequest, ...], tuple[str, ...]]:
    """Collect the GPU requests and CDI device names in a Compose configuration.

    Reads ``gpus``, ``deploy.resources.reservations.devices`` and ``devices`` of
    each service, in service order. A reservation counts when a capability is one
    of the NVIDIA (``gpu``, ``nvidia``, ``compute``, ``compat32``, ``graphics``,
    ``utility``, ``video``, ``display``) or AMD (``gpu``, ``amd``) capabilities, or when
    the driver is ``nvidia`` or ``cdi``. A request with no count and no device IDs
    asks for all GPUs. A ``devices`` entry counts only when it is a mapping whose
    ``source`` and ``target`` are the same qualified CDI name; other entries, such as
    host mappings, are ignored. Shapes that don't match are skipped silently.

    Args:
        services: The ``services`` mapping of normalized
            ``docker compose config --format json`` output.

    Returns:
        The GPU requests and the qualified CDI device names, each in the order
        found; names aren't deduplicated.
    """
    requests: list[GpuRequest] = []
    names: list[str] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        gpus = service.get("gpus")
        if gpus == "all":
            requests.append(GpuRequest(count=-1))
        elif isinstance(gpus, list):
            requests.extend(
                request for entry in gpus if (request := _compose_request(entry))
            )
        for entry in _reserved_devices(service):
            if (
                isinstance(entry, dict)
                and _wants_gpu(entry)
                and (request := _compose_request(entry)) is not None
            ):
                requests.append(request)
        devices = service.get("devices")
        if isinstance(devices, list):
            names.extend(
                name
                for item in devices
                if (name := _compose_device_name(item)) is not None
            )
    return tuple(requests), tuple(names)


def _schedulable(node: Node) -> bool:
    """Whether a Swarm node is ready and accepts new tasks."""
    status, spec = node.status, node.spec
    return (
        status is not None
        and status.state == "ready"
        and spec is not None
        and spec.availability == "active"
    )


def _advertised_resources(node: Node) -> Counter[str]:
    """Return the generic resource totals a node advertises, by kind.

    Discrete entries add their value; each named entry counts as one.
    """
    totals: Counter[str] = Counter()
    description = node.description
    resources = description.resources if description is not None else None
    for resource in (resources.generic_resources if resources else None) or ():
        discrete = resource.discrete_resource_spec
        if (
            discrete is not None
            and discrete.kind is not None
            and discrete.value is not None
        ):
            totals[discrete.kind] += discrete.value
        named = resource.named_resource_spec
        if named is not None and named.kind is not None:
            totals[named.kind] += 1
    return totals


def generic_resource_requests(requests: Iterable[str]) -> dict[str, int]:
    """Return the amounts a set of Swarm generic resource requests asks for.

    Each request is ``kind=value`` with a non-negative decimal value, such as
    ``gpu=2``; like swarmkit's integer parse, a leading ``+`` is accepted.
    Malformed entries are skipped, since the CLI rejects them. Duplicate kinds are
    summed here, although the Docker CLI rejects them, and zero amounts dropped.

    Args:
        requests: Generic resource requests such as ``"gpu=2"``. Consumed once.

    Returns:
        The summed nonzero amount per kind, in first-seen order. Empty when no
        entry has the ``kind=value`` shape with a nonzero amount.
    """
    totals: Counter[str] = Counter()
    for request in requests:
        kind, separator, amount = request.partition("=")
        kind, amount = kind.strip(), amount.strip()
        if separator and kind and _GENERIC_AMOUNT.fullmatch(amount) is not None:
            totals[kind] += int(amount)
    return {kind: amount for kind, amount in totals.items() if amount}


def check_generic_resources(requests: Iterable[str], nodes: Iterable[Node]) -> None:
    """Raise unless one ready, active Swarm node advertises every requested resource.

    Each request is ``kind=value`` with a non-negative decimal value, such as
    ``gpu=2``, optionally with a leading ``+``. Malformed entries are skipped, since
    the CLI rejects them and named requests aren't supported for services.
    Duplicate kinds are summed here, although the Docker CLI rejects them, and zero
    amounts dropped. A node's capacity per kind is the sum of its discrete generic
    resource values plus its count of named ones.

    This compares advertised capacity, not what remains after running tasks, and
    ignores placement constraints. It can pass a request the scheduler later
    leaves pending, but never refuses one a ready, active node could serve.

    Node properties are read through each handle, so pass hydrated handles (for
    example from ``list(hydrate=True)``) to avoid one inspect launch per read.

    Args:
        requests: Generic resource requests such as ``"gpu=2"``. Consumed once.
        nodes: Swarm nodes to consider. Consumed once.

    Raises:
        GpuSupportError: With reason ``"no_node_capacity"`` when no eligible node
            satisfies every request. Its `devices` are the normalized
            ``kind=value`` requests no eligible node satisfies alone, in
            first-seen order, or all of them when each fits some node but none
            fits them together.
    """
    wanted = generic_resource_requests(requests)
    if not wanted:
        return
    capacities = [_advertised_resources(node) for node in nodes if _schedulable(node)]
    if any(
        all(capacity[kind] >= amount for kind, amount in wanted.items())
        for capacity in capacities
    ):
        return
    unsatisfied = [
        kind
        for kind, amount in wanted.items()
        if not any(capacity[kind] >= amount for capacity in capacities)
    ]
    raise GpuSupportError(
        "no_node_capacity",
        tuple(f"{kind}={wanted[kind]}" for kind in unsatisfied or wanted),
    )
