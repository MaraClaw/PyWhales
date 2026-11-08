"""Exact PyPI destination inventory and safe partial-publication retries."""

import os
import time
from pathlib import Path
from typing import Final

from .api import GitHub, require
from .chain import producer, publisher_identity
from .deadline import afford
from .models import PyPIRelease, Receipt, Seal

CONFIRM_RETRIES: Final = 12
CONFIRM_RETRY_DELAY_SECONDS: Final = 5


def destination(api: GitHub, seal: Seal) -> set[str]:
    """Reject conflicts and return only filenames absent from the target version."""
    response = api.transport.request(
        "GET", f"https://pypi.org/pypi/pywhales/{seal.version}/json"
    )
    require(response.status in {200, 404}, "pypi response")
    existing = (
        ()
        if response.status == 404
        else PyPIRelease.model_validate_json(response.body).urls
    )
    require(
        len({item.filename for item in existing}) == len(existing),
        "duplicate destination file",
    )
    require(
        all(item.filename in seal.distributions.files for item in existing),
        "unexpected destination file",
    )
    for item in existing:
        kind = "bdist_wheel" if item.filename.endswith(".whl") else "sdist"
        require(
            item.packagetype == kind
            and item.digests.sha256 == seal.distributions.files[item.filename],
            "destination content mismatch",
        )
    return set(seal.distributions.files) - {item.filename for item in existing}


def prepare(api: GitHub, run_id: int, output: Path) -> Seal:
    """Write exact missing distributions only, after fully verifying the producer."""
    tested, _artifact, distributions = producer(api, run_id)
    require(
        tested.producer.attempt == int(os.environ["PRODUCER_ATTEMPT"]),
        "triggering producer attempt mismatch",
    )
    missing = destination(api, tested)
    output.mkdir(parents=True, exist_ok=False)
    for filename in sorted(missing):
        (output / filename).write_bytes(distributions[filename])
    return tested


def confirm(api: GitHub, run_id: int) -> Receipt:
    """Issue a receipt only when both exact destination files are present.

    A skipped or failed uploader is not evidence. A partial or concurrent retry
    is accepted only when the independent destination check is complete.
    PyPI's index can briefly lag a successful upload, so a nonempty missing set
    is retried up to CONFIRM_RETRIES times, CONFIRM_RETRY_DELAY_SECONDS apart,
    rechecking the complete destination inventory (kinds, duplicates, hashes)
    on every attempt. A conflicting, invalid, or malformed destination response
    fails immediately and is never retried. Polling shares the command
    deadline: a wait that would not finish before it is rejected, unslept.
    """
    tested, artifact, _distributions = producer(api, run_id)
    require(
        tested.producer.attempt == int(os.environ["PRODUCER_ATTEMPT"]),
        "triggering producer attempt mismatch",
    )
    missing = destination(api, tested)
    for _ in range(CONFIRM_RETRIES):
        if not missing:
            break
        afford(CONFIRM_RETRY_DELAY_SECONDS)
        time.sleep(CONFIRM_RETRY_DELAY_SECONDS)
        missing = destination(api, tested)
    require(not missing, f"destination publication incomplete: {sorted(missing)}")
    return Receipt(
        publisher=publisher_identity(api),
        seal=tested,
        seal_artifact=artifact,
        destination="https://pypi.org",
        confirmed=True,
    )
