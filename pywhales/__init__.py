"""Public Docker CLI clients, resource handles, models, and enums.

Importing this package constructs the shared ``docker`` client without looking up
the executable or contacting a daemon. Operations resolve their CLI prerequisites
when needed.
"""

from . import docker_client as docker_client
from . import enums
from ._observation import observation
from .client_config import ClientNotFoundError
from .components.buildx.cli_wrapper import Builder
from .components.config.cli_wrapper import Config
from .components.container.cli_wrapper import Container, ContainerStats
from .components.context.cli_wrapper import (
    Context,
    DockerContextConfig,
    KubernetesContextConfig,
)
from .components.image.cli_wrapper import Image
from .components.network.cli_wrapper import Network
from .components.node.cli_wrapper import Node
from .components.plugin.cli_wrapper import Plugin
from .components.secret.cli_wrapper import Secret
from .components.service.cli_wrapper import Service
from .components.stack.cli_wrapper import Stack
from .components.system.cli_wrapper import SystemInfo
from .components.task.cli_wrapper import Task
from .components.volume.cli_wrapper import Volume
from .docker_client import DockerClient, Version
from .exceptions import (
    DockerException,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
    StreamBufferLimitError,
)
from .inventory import (
    InspectionPolicy,
    InventoryKind,
    InventoryPayload,
    InventoryRecord,
    InventoryRequest,
    InventoryResource,
    InventorySnapshot,
    capture_inventory,
)

# alias
docker = DockerClient(client_type="docker")

__all__ = [
    "Builder",
    "ClientNotFoundError",
    "Config",
    "Container",
    "ContainerStats",
    "Context",
    "DockerClient",
    "DockerContextConfig",
    "DockerException",
    "Image",
    "InspectionPolicy",
    "InventoryKind",
    "InventoryPayload",
    "InventoryRecord",
    "InventoryRequest",
    "InventoryResource",
    "InventorySnapshot",
    "KubernetesContextConfig",
    "Network",
    "Node",
    "ObservationCancelled",
    "ObservationLimitError",
    "ObservationTimeout",
    "Plugin",
    "Secret",
    "Service",
    "Stack",
    "StreamBufferLimitError",
    "SystemInfo",
    "Task",
    "Version",
    "Volume",
    "capture_inventory",
    "enums",
    "observation",
]
