"""Inventory request and snapshot value types.

Capture is loaded only when requested; constructing these values does not locate
the Docker executable or inspect resources.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from pywhales.client_config import ClientConfig
from pywhales.components.buildx.cli_wrapper import Builder
from pywhales.components.buildx.models import BuilderInspectResult
from pywhales.components.config.cli_wrapper import Config
from pywhales.components.config.models import ConfigInspectResult
from pywhales.components.container.cli_wrapper import Container
from pywhales.components.container.models import ContainerInspectResult
from pywhales.components.context.cli_wrapper import Context
from pywhales.components.context.models import ContextInspectResult
from pywhales.components.image.cli_wrapper import Image
from pywhales.components.image.models import ImageInspectResult
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.components.manifest.models import ManifestListInspectResult
from pywhales.components.network.cli_wrapper import Network
from pywhales.components.network.models import NetworkInspectResult
from pywhales.components.node.cli_wrapper import Node
from pywhales.components.node.models import NodeInspectResult
from pywhales.components.plugin.cli_wrapper import Plugin
from pywhales.components.plugin.models import PluginInspectResult
from pywhales.components.secret.cli_wrapper import Secret
from pywhales.components.secret.models import SecretInspectResult
from pywhales.components.service.cli_wrapper import Service
from pywhales.components.service.models import ServiceInspectResult
from pywhales.components.task.cli_wrapper import Task
from pywhales.components.task.models import TaskInspectResult
from pywhales.components.volume.cli_wrapper import Volume
from pywhales.components.volume.models import VolumeInspectResult

type InventoryKind = Literal[
    "container",
    "image",
    "network",
    "volume",
    "node",
    "service",
    "config",
    "secret",
    "task",
    "plugin",
    "context",
    "builder",
    "manifest_list",
]
type InventoryResource = (
    Container
    | Image
    | Network
    | Volume
    | Node
    | Service
    | Config
    | Secret
    | Task
    | Plugin
    | Context
    | Builder
    | ManifestList
)
type InventoryPayload = (
    ContainerInspectResult
    | ImageInspectResult
    | NetworkInspectResult
    | VolumeInspectResult
    | NodeInspectResult
    | ServiceInspectResult
    | ConfigInspectResult
    | SecretInspectResult
    | TaskInspectResult
    | PluginInspectResult
    | ContextInspectResult
    | BuilderInspectResult
    | ManifestListInspectResult
)


def _valid_references(references: object) -> bool:
    return (
        isinstance(references, tuple)
        and bool(references)
        and all(
            isinstance(reference, str) and bool(reference) for reference in references
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectionPolicy:
    """Positive caller-supplied limits for an observation.

    ``max_total_stream_bytes`` caps the stdout and stderr bytes that streams and
    archive transfers started inside the scope read; captured output counts
    against ``max_total_response_bytes`` instead. ``max_total_stdin_bytes`` caps
    the bytes that captured runs and archive uploads started inside the scope
    write to child stdin, counting only bytes the local pipe accepted; it is
    independent of both output totals, and a captured ``run()`` may send input
    only while a scope sets it. ``max_total_io_bytes`` is one further shared
    allowance over captured, stream and archive output reads and managed stdin
    writes (captured input and archive uploads) inside the scope; it adds to
    those budgets and never replaces them. An output read may take one byte past
    it, which fails with reason ``total_io``, and nothing spends it after that.
    ``max_total_launch_attempts`` caps accepted CLI launch reservations after
    cancellation, deadline, and argument preflight. Failed or interrupted
    launches are not refunded. ``max_concurrent_children`` caps owned local CLI
    children until reap, independently of output draining or daemon workloads. A
    failed spawn returns its child slot, not its attempt. ``None`` leaves an
    optional limit unset.
    """

    max_references: int
    max_argv_bytes: int
    max_response_bytes: int
    max_total_references: int | None = None
    max_total_response_bytes: int | None = None
    max_total_stream_bytes: int | None = None
    max_total_stdin_bytes: int | None = None
    max_total_io_bytes: int | None = None
    max_total_launch_attempts: int | None = None
    max_concurrent_children: int | None = None

    def __post_init__(self) -> None:
        """Reject non-integer, boolean, and nonpositive limits."""
        for value in (
            self.max_references,
            self.max_argv_bytes,
            self.max_response_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("Inspection limits must be positive integers")
        for value in (
            self.max_total_references,
            self.max_total_response_bytes,
            self.max_total_stream_bytes,
            self.max_total_stdin_bytes,
            self.max_total_io_bytes,
            self.max_total_launch_attempts,
            self.max_concurrent_children,
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Inspection limits must be positive integers")


@dataclass(frozen=True, slots=True, kw_only=True)
class InventoryRequest:
    """References of one kind using the exact originating client configuration."""

    kind: InventoryKind
    references: tuple[str, ...]
    client_config: ClientConfig

    def __post_init__(self) -> None:
        """Validate request shape without resolving the executable."""
        if self.kind not in get_args(InventoryKind.__value__):
            raise ValueError("Unsupported inventory kind")
        if not _valid_references(self.references):
            raise ValueError("Inventory references must be nonempty strings")


@dataclass(frozen=True, slots=True, kw_only=True)
class InventoryRecord:
    """An inspected resource identity and its concrete inspect model."""

    kind: InventoryKind
    id: str
    payload: InventoryPayload


@dataclass(frozen=True, slots=True, kw_only=True)
class InventorySnapshot:
    """Ordered inventory records and their observation interval."""

    records: tuple[InventoryRecord, ...]
    started_at: datetime
    finished_at: datetime


def capture_inventory(
    items: Sequence[InventoryRequest | InventoryResource], *, policy: InspectionPolicy
) -> InventorySnapshot:
    """Capture an inventory using the separately supplied capture engine.

    Args:
        items: Requests or existing resource handles to inspect.
        policy: Explicit bounds for the capture.

    Returns:
        The capture engine's ordered snapshot.
    """
    from pywhales._inventory_capture import capture_inventory as capture

    return capture(items, policy=policy)
