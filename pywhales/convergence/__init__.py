"""Opt-in observation of fixed Swarm service generations and Compose projects."""

from pywhales.convergence._types import (
    Cancelled,
    ComposeService,
    ComposeTarget,
    ContainerTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    NodeTarget,
    Outcome,
    ServiceTarget,
    StackTarget,
    Success,
)
from pywhales.convergence._wait import (
    capture_compose,
    capture_containers,
    capture_nodes,
    capture_service,
    wait,
)

__all__ = [
    "Cancelled",
    "ComposeService",
    "ComposeTarget",
    "ContainerTarget",
    "Deadline",
    "DeadlineDetail",
    "Failure",
    "NodeTarget",
    "Outcome",
    "ServiceTarget",
    "StackTarget",
    "Success",
    "capture_compose",
    "capture_containers",
    "capture_nodes",
    "capture_service",
    "wait",
]
