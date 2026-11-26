"""Typed GitHub, PyPI and delivery-document boundaries."""

from base64 import b64decode
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import limits

type Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
type Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
type Positive = Annotated[int, Field(gt=0)]
type Version = Annotated[
    str,
    Field(
        pattern=r"^[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?$"
    ),
]


class APIModel(BaseModel):
    """Parse known REST fields while allowing API extensions."""

    model_config = ConfigDict(strict=True, frozen=True)


class Document(APIModel):
    """Reject unknown delivery-document fields."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Repository(APIModel):
    """Authoritative repository identity."""

    id: Positive
    full_name: str
    default_branch: str | None = None


class Run(APIModel):
    """Authoritative workflow attempt identity."""

    id: Positive
    run_attempt: Positive
    workflow_id: Positive
    path: str
    event: str
    head_branch: str
    head_sha: Sha
    repository: Repository
    head_repository: Repository
    status: str
    conclusion: str | None


class Workflow(APIModel):
    """Workflow ID resolved through its expected filename."""

    id: Positive
    path: str
    state: str


class Job(APIModel):
    """Attempt-scoped job with its artifact creation interval."""

    id: Positive
    run_id: Positive
    run_attempt: Positive | None = None
    head_sha: Sha
    name: str
    status: str
    conclusion: str | None
    started_at: datetime
    completed_at: datetime | None


class Jobs(APIModel):
    """One page of authoritative jobs with the listing's `total_count`."""

    total_count: Annotated[int, Field(ge=0)]
    jobs: tuple[Job, ...]


class ArtifactRun(APIModel):
    """GitHub-provided artifact linkage, not manifest claims."""

    id: Positive
    repository_id: Positive
    head_repository_id: Positive
    head_branch: str
    head_sha: Sha


class Artifact(APIModel):
    """Immutable uploaded ZIP identity."""

    id: Positive
    name: str
    expired: bool
    digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    created_at: datetime
    updated_at: datetime
    workflow_run: ArtifactRun


class Artifacts(APIModel):
    """One page of run-scoped artifacts with the listing's `total_count`."""

    total_count: Annotated[int, Field(ge=0)]
    artifacts: tuple[Artifact, ...]


class Identity(Document):
    """Pinned workflow/run/attempt/source identity."""

    repository: str
    repository_id: Positive
    workflow_id: Positive
    workflow_path: str
    run_id: Positive
    attempt: Positive
    sha: Sha


class Bundle(Document):
    """An uploaded ZIP and every contained file digest."""

    artifact_id: Positive
    name: str
    archive_sha256: Digest
    files: dict[str, Digest]


class Seal(Document):
    """Eligible tested package/site bytes from one complete attempt."""

    schema_version: Literal[2] = 2
    producer: Identity
    jobs: dict[str, Positive]
    version: Version
    notes: str
    distributions: Bundle
    site: Bundle

    @field_validator("notes")
    @classmethod
    def sized(cls, value: str) -> str:
        """Bound the release body at the document boundary, not only at sealing."""
        if not value.strip() or len(value.encode()) > limits.NOTES_BYTES:
            raise ValueError("release notes empty or over the size budget")
        return value


class Receipt(Document):
    """Publication evidence issued only after PyPI file confirmation."""

    schema_version: Literal[2] = 2
    publisher: Identity
    seal: Seal
    seal_artifact: Bundle
    destination: Literal["https://pypi.org"]
    confirmed: Literal[True]


class Base(APIModel):
    """Pull request base branch and repository."""

    ref: str
    repo: Repository


class Pull(APIModel):
    """Authoritative merged pull request eligibility."""

    merged: bool
    merge_commit_sha: str | None
    base: Base


class PullNumber(APIModel):
    """Commit-associated pull request reference."""

    number: Positive


class PyPIDigest(APIModel):
    """PyPI's destination content hash."""

    sha256: Digest


class PyPIFile(APIModel):
    """Destination filename, kind and hash."""

    filename: str
    packagetype: str
    digests: PyPIDigest


class PyPIRelease(APIModel):
    """Version-specific PyPI file inventory."""

    urls: tuple[PyPIFile, ...]


class PyPIPublisher(APIModel):
    """GitHub-shaped Trusted Publisher identity reported by PyPI."""

    kind: str
    repository: str
    workflow: str
    environment: str | None


class PyPISubject(APIModel):
    """PEP 740 binding between a distribution filename and its digest."""

    name: str
    digest: PyPIDigest


class PyPIStatement(APIModel):
    """The single-subject in-toto v1 statement used by PEP 740."""

    type: Literal["https://in-toto.io/Statement/v1"] = Field(alias="_type")
    subject: tuple[PyPISubject]


class PyPIEnvelope(APIModel):
    """Envelope with its statement decoded at the API boundary."""

    statement: PyPIStatement

    @field_validator("statement", mode="before")
    @classmethod
    def decode_statement(cls, value: object) -> PyPIStatement:
        """Parse the base64-encoded JSON statement without coercing input types."""
        if not isinstance(value, str):
            raise ValueError("expected a base64 statement")
        return PyPIStatement.model_validate_json(b64decode(value, validate=True))


class PyPIAttestation(APIModel):
    """Version-1 attestation fields used in the confirmation decision."""

    version: Annotated[int, Field(ge=1, le=1)]
    envelope: PyPIEnvelope


class PyPIAttestationBundle(APIModel):
    """A publisher and the attestations PyPI associates with it."""

    publisher: PyPIPublisher
    attestations: Annotated[tuple[PyPIAttestation, ...], Field(min_length=1)]


class PyPIProvenance(APIModel):
    """Version-1 per-file response from PyPI's Integrity API."""

    version: Annotated[int, Field(ge=1, le=1)]
    attestation_bundles: Annotated[
        tuple[PyPIAttestationBundle, ...], Field(min_length=1)
    ]


class GitObject(APIModel):
    """A commit or annotated tag object."""

    type: str
    sha: Sha


class GitRef(APIModel):
    """Git ref or annotated tag target."""

    object: GitObject


class Release(APIModel):
    """Published GitHub release state."""

    id: Positive
    tag_name: str
    draft: bool
    prerelease: bool
    body: str | None = None
