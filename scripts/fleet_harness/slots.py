"""Exact fake asset identities, allocated or looked up at event time."""

from .fake import FakeDriver
from .models import RunnableManifest, Step
from .records import OwnedResource


class AssetLimit(Exception):
    """Fixed signal that the created-asset ceiling forbids another identity."""


class Slots:
    """Manifest-declared asset slots; identities are allocated, never inferred."""

    def __init__(self, manifest: RunnableManifest) -> None:
        """Copy the slot kinds, endpoint scopes, ownership scope and asset ceiling."""
        auth = manifest.authorization
        self.assets = {asset.id: asset for asset in manifest.ownership.assets}
        self.endpoints = {
            alias.name: alias.effective_endpoint for alias in manifest.platform.aliases
        }
        self.namespace, self.marker = auth.run_namespace, auth.ownership_marker
        self.limit = min(
            auth.maximum_created_assets, manifest.ownership.maximum_created_assets
        )

    def resolve(
        self, step: Step, submission: int, driver: FakeDriver
    ) -> OwnedResource | None:
        """Return the latest live identity of the slot, or allocate one for a creation.

        The lookup reads the driver's inventory as of the current event, so an
        overlapping removal or creation is observed in chronological order.
        """
        if step.asset is None:
            return None
        endpoint = self.endpoints[step.endpoint]
        resource = next(
            (
                item
                for item in reversed(driver.live)
                if item.slot == step.asset and item.endpoint == endpoint
            ),
            None,
        )
        if step.action in {"create", "pull", "build"}:
            if len(driver.ledger) >= self.limit:
                raise AssetLimit
            resource = OwnedResource(
                endpoint=endpoint,
                kind=self.assets[step.asset].kind,
                identity=f"fake-{submission}",
                slot=step.asset,
                namespace=self.namespace,
                marker=self.marker,
            )
        return resource
