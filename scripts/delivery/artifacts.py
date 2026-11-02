"""Verify archive digests before safe data-only ZIP inspection and extraction."""

import hashlib
import tempfile
from pathlib import Path

from . import limits
from .api import GitHub, require
from .archives import safe_path, zip_files
from .models import Artifact, Bundle, Job, Run


def sha256(content: bytes) -> str:
    """Hash exact bytes, including the ZIP wrapper when used for downloads."""
    return hashlib.sha256(content).hexdigest()


def inventory(api: GitHub, run: Run) -> tuple[Artifact, ...]:
    """Enumerate the entire run-scoped inventory; names distinguish attempts."""
    return api.pages(f"actions/runs/{run.id}/artifacts", Artifact, "artifacts")


def select(artifacts: tuple[Artifact, ...], name: str) -> Artifact:
    """Reject absent, duplicate and expired evidence instead of choosing one."""
    matches = [artifact for artifact in artifacts if artifact.name == name]
    require(
        bool(matches),
        f"missing artifact: {name}; use 'Re-run all jobs' on the producing workflow "
        "so artifacts and checks belong to the same attempt",
    )
    require(len(matches) == 1, f"ambiguous artifact: {name}")
    require(not matches[0].expired, "expired artifact")
    return matches[0]


def linked(artifact: Artifact, run: Run, job: Job) -> None:
    """Bind run-scoped material to its same-attempt producing job interval."""
    link = artifact.workflow_run
    require(
        link.id == run.id
        and link.repository_id == run.repository.id
        and link.head_repository_id == run.head_repository.id
        and link.head_sha == run.head_sha
        and link.head_branch == "main",
        "artifact run linkage",
    )
    require(
        job.completed_at is not None
        and job.started_at
        <= artifact.created_at
        <= artifact.updated_at
        <= job.completed_at,
        "artifact attempt interval",
    )


def download(api: GitHub, artifact: Artifact) -> dict[str, bytes]:
    """Check the authoritative ZIP digest, then reject unsafe or ambiguous entries."""
    response = api.response(f"actions/artifacts/{artifact.id}/zip")
    require(response.status == 200, "artifact download failed")
    require(
        len(response.body) <= limits.RESPONSE_BYTES,
        "artifact input byte budget exceeded",
    )
    require(
        f"sha256:{sha256(response.body)}" == artifact.digest, "archive digest mismatch"
    )
    files = zip_files(response.body, limits.ARTIFACT)
    require(bool(files), "empty artifact")
    return files


def describe(artifact: Artifact, files: dict[str, bytes]) -> Bundle:
    """Bind the ZIP identity to all extracted file hashes."""
    return Bundle(
        artifact_id=artifact.id,
        name=artifact.name,
        archive_sha256=artifact.digest.removeprefix("sha256:"),
        files={name: sha256(content) for name, content in files.items()},
    )


def verified(api: GitHub, artifact: Artifact, expected: Bundle) -> dict[str, bytes]:
    """Verify every byte and forbid additional or missing files."""
    files = download(api, artifact)
    require(
        describe(artifact, files) == expected, "artifact content or identity mismatch"
    )
    return files


def extract(files: dict[str, bytes], destination: Path) -> None:
    """Stage validated regular files; a rejected extraction publishes nothing."""
    require(not destination.exists(), "extraction destination exists")
    require(len(files) <= limits.ARTIFACT.entries, "archive entry budget exceeded")
    require(
        sum(map(len, files.values())) <= limits.ARTIFACT.expanded_bytes,
        "archive byte budget exceeded",
    )
    for name, content in files.items():
        safe_path(name)
        require(
            len(content) <= limits.ARTIFACT.member_bytes, "archive byte budget exceeded"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".delivery-", dir=destination.parent
    ) as temporary:
        staged = Path(temporary) / "files"
        staged.mkdir()
        for name, content in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        staged.rename(destination)
