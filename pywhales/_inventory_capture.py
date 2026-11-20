"""Capture detached inventory records within an observation interval."""

from collections.abc import Sequence
from datetime import UTC, datetime

from pywhales._inventory_staging import stage_records
from pywhales.inventory import (
    InspectionPolicy,
    InventoryRequest,
    InventoryResource,
    InventorySnapshot,
)


def capture_inventory(
    items: Sequence[InventoryRequest | InventoryResource], *, policy: InspectionPolicy
) -> InventorySnapshot:
    """Return detached, ordered records without modifying input handles."""
    started_at = datetime.now(UTC)
    records = stage_records(items, policy)
    return InventorySnapshot(
        records=tuple(records), started_at=started_at, finished_at=datetime.now(UTC)
    )
