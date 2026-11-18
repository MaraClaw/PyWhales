"""GPU capability of the selected Docker daemon, derived from its reported info.

Everything here describes the daemon the client is configured for (its context,
host or SSH endpoint), never the machine running Python. Nothing inspects local
devices or drivers.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pywhales.components.system.models import SystemInfo
from pywhales.exceptions import GpuSupportError

GPU_DEVICE_KINDS: frozenset[str] = frozenset({"nvidia.com/gpu", "amd.com/gpu"})
"""CDI device kinds that Docker's `--gpus` option can inject."""

GPU_RUNTIMES: frozenset[str] = frozenset({"nvidia"})
"""Registered runtime names that indicate an installed GPU container toolkit."""

_CDI_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9_-]*=[A-Za-z0-9][A-Za-z0-9._:-]*"
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
    def gpus_available(self) -> bool:
        """Whether the daemon runs Linux containers and reports a GPU path."""
        return self.os_type == "linux" and bool(self.gpu_runtimes or self.gpu_devices)

    def check(self, *, gpus: int | str | None, devices: Iterable[str] = ()) -> None:
        """Raise unless the daemon reports support for the given GPU requests.

        Args:
            gpus: The `--gpus` value, or `None` when not requested.
            devices: `--device` values; only qualified CDI names are checked,
                and each must appear verbatim among `cdi_devices`.

        Raises:
            GpuSupportError: If `gpus` is requested without `gpus_available`, or
                a requested CDI device isn't reported. A non-Linux daemon fails
                either request with reason ``"not_linux"``.
        """
        cdi_requests = [
            device for device in devices if cdi_device_kind(device) is not None
        ]
        if gpus is None and not cdi_requests:
            return
        if self.os_type != "linux":
            raise GpuSupportError("not_linux")
        if gpus is not None and not self.gpus_available:
            raise GpuSupportError("no_gpu_driver")
        if not cdi_requests:
            return
        if self.cdi_devices is None:
            raise GpuSupportError("cdi_devices_unreported")
        known = set(self.cdi_devices)
        missing = tuple(device for device in cdi_requests if device not in known)
        if missing:
            raise GpuSupportError("cdi_device_missing", missing)
