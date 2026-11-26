"""Exact PyPI destination inventory and safe partial-publication retries."""

import os
import time
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from . import limits
from .api import GitHub, Rejected, require
from .chain import producer, publisher_identity
from .deadline import afford
from .models import PyPIProvenance, PyPIRelease, Receipt, Seal
from .trust import PUBLISHER

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


def verify_provenance(api: GitHub, seal: Seal) -> None:
    """Require matching PyPI-reported publisher and subject evidence per file."""
    for filename, digest in sorted(seal.distributions.files.items()):
        response = api.transport.request(
            "GET",
            f"https://pypi.org/integrity/pywhales/{seal.version}/{filename}/provenance",
        )
        require(response.status != 404, "pypi provenance not found")
        require(response.status == 200, "pypi provenance response")
        require(
            len(response.body) <= limits.RESPONSE_BYTES,
            "HTTP response byte budget exceeded",
        )
        try:
            provenance = PyPIProvenance.model_validate_json(response.body)
        except ValidationError:
            raise Rejected("pypi provenance malformed") from None
        github = [
            bundle
            for bundle in provenance.attestation_bundles
            if bundle.publisher.kind == "GitHub"
        ]
        require(bool(github), "pypi provenance publisher kind mismatch")
        repositories = [
            bundle for bundle in github if bundle.publisher.repository == api.repository
        ]
        require(bool(repositories), "pypi provenance repository mismatch")
        workflows = [
            bundle for bundle in repositories if bundle.publisher.workflow == PUBLISHER
        ]
        require(bool(workflows), "pypi provenance workflow mismatch")
        require(
            any(
                subject.name == filename and subject.digest.sha256 == digest
                for bundle in workflows
                for attestation in bundle.attestations
                for subject in attestation.envelope.statement.subject
            ),
            "pypi provenance subject mismatch",
        )


def confirm(api: GitHub, run_id: int) -> Receipt:
    """Require both exact destination files and their PyPI provenance.

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
    verify_provenance(api, tested)
    return Receipt(
        publisher=publisher_identity(api),
        seal=tested,
        seal_artifact=artifact,
        destination="https://pypi.org",
        confirmed=True,
    )
