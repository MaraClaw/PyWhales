"""Producer sealing and independently reverified receipt consumption."""

import os
import tomllib
from pathlib import Path

from . import limits
from .api import GitHub, Rejected, require
from .artifacts import describe, download, inventory, linked, select, verified
from .budgets import local
from .distributions import inspect
from .models import Artifact, Bundle, Identity, Job, Receipt, Run, Seal
from .notes import section
from .trust import (
    CHECKS,
    PRODUCER,
    PUBLISH_JOB,
    PUBLISHER,
    SEAL_JOB,
    attempt_jobs,
    identity,
    merged_pull,
    successful_jobs,
    trusted_run,
)


def name(kind: str, run: Run) -> str:
    """Qualify every artifact name by both run and attempt."""
    return f"delivery-{kind}-{run.id}-{run.run_attempt}"


def job_named(jobs: tuple[Job, ...], name: str) -> Job:
    """Select a job only after the complete unique job set was validated."""
    return next(job for job in jobs if job.name == name)


def bundle(api: GitHub, run: Run, jobs: tuple[Job, ...], kind: str) -> Artifact:
    """Select and link one known artifact kind to its producing job."""
    owners = {
        "dist": "Build tested distributions",
        "site": "Render docs",
        "seal": SEAL_JOB,
        "receipt": PUBLISH_JOB,
    }
    artifact = select(inventory(api, run), name(kind, run))
    linked(artifact, run, job_named(jobs, owners[kind]))
    return artifact


def release_notes(root: Path, version: str) -> str:
    """Read the sealed checkout's CHANGELOG section for exactly this version."""
    try:
        text = local(root / "CHANGELOG.md", limits.CHANGELOG_BYTES).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError:
        raise Rejected("changelog is not UTF-8") from None
    return section(text, version)


def project_version(root: Path) -> str:
    """Read the version declared by the checkout's bounded pyproject.toml."""
    project = tomllib.loads(
        local(root / "pyproject.toml", limits.PROJECT_BYTES).decode("utf-8")
    ).get("project")
    version = project.get("version") if isinstance(project, dict) else None
    require(isinstance(version, str), "project version missing")
    assert isinstance(version, str)
    return version


def seal(api: GitHub, run_id: int, root: Path) -> Seal:
    """Seal after all existing checks succeeded in the current producer attempt.

    This job is still running. Consumers additionally require its successful
    completion and recheck all the authoritative jobs and archive identities.
    """
    run = api.get(
        f"actions/runs/{run_id}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}", Run
    )
    require(
        run.id == run_id
        and run.run_attempt == int(os.environ["GITHUB_RUN_ATTEMPT"])
        and run.head_sha == os.environ["GITHUB_SHA"],
        "sealer context",
    )
    jobs = attempt_jobs(api, run)
    require(
        {job.name for job in jobs} == {*CHECKS, SEAL_JOB}, "incomplete sealer attempt"
    )
    require(
        all(
            job.conclusion == "success" and job.completed_at is not None
            for job in jobs
            if job.name != SEAL_JOB
        ),
        "sealer checks failed",
    )
    dist = bundle(api, run, jobs, "dist")
    site = bundle(api, run, jobs, "site")
    for kind, artifact in (("DIST", dist), ("SITE", site)):
        require(
            artifact.id == int(os.environ[f"{kind}_ARTIFACT_ID"])
            and artifact.digest == f"sha256:{os.environ[f'{kind}_ARTIFACT_DIGEST']}",
            "uploader output linkage",
        )
    distributions = download(api, dist)
    version = project_version(root)
    notes = release_notes(root, version)
    inspect(distributions, version)
    return Seal(
        producer=identity(run),
        jobs={job.name: job.id for job in jobs},
        version=version,
        notes=notes,
        distributions=describe(dist, distributions),
        site=describe(site, download(api, site)),
    )


def producer(api: GitHub, run_id: int) -> tuple[Seal, Bundle, dict[str, bytes]]:
    """Reconstruct a trusted tested chain before any privileged publication."""
    run = trusted_run(api, run_id, PRODUCER)
    merged_pull(api, run)
    jobs = successful_jobs(api, run, (*CHECKS, SEAL_JOB))
    artifact = bundle(api, run, jobs, "seal")
    files = download(api, artifact)
    require(set(files) == {"seal.json"}, "seal file set")
    document = Seal.model_validate_json(files["seal.json"])
    require(document.producer == identity(run), "seal producer identity")
    require(document.jobs == {job.name: job.id for job in jobs}, "seal job identity")
    dist = verified(api, bundle(api, run, jobs, "dist"), document.distributions)
    inspect(dist, document.version)
    verified(api, bundle(api, run, jobs, "site"), document.site)
    return document, describe(artifact, files), dist


def publisher_identity(api: GitHub) -> Identity:
    """Bind the current receipt to its authoritative running publisher attempt."""
    run_id = int(os.environ["GITHUB_RUN_ID"])
    run = api.get(
        f"actions/runs/{run_id}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}", Run
    )
    require(
        run.id == run_id
        and run.run_attempt == int(os.environ["GITHUB_RUN_ATTEMPT"])
        and run.head_sha == os.environ["GITHUB_SHA"]
        and run.path
        in {
            f".github/workflows/{PUBLISHER}",
            f".github/workflows/{PUBLISHER}@main",
            f".github/workflows/{PUBLISHER}@refs/heads/main",
        },
        "publisher context",
    )
    return identity(run)


def receipt(api: GitHub, run_id: int) -> Receipt:
    """Validate a completed publisher receipt identically for automatic/manual use."""
    run = trusted_run(api, run_id, PUBLISHER)
    jobs = successful_jobs(api, run, (PUBLISH_JOB,))
    artifact = bundle(api, run, jobs, "receipt")
    files = download(api, artifact)
    require(set(files) == {"receipt.json"}, "receipt file set")
    document = Receipt.model_validate_json(files["receipt.json"])
    require(document.publisher == identity(run), "receipt publisher identity")
    tested, seal_artifact, _dist = producer(api, document.seal.producer.run_id)
    require(
        tested == document.seal and seal_artifact == document.seal_artifact,
        "receipt tested chain mismatch",
    )
    return document
