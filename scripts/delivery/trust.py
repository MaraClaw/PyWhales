"""Authoritative workflow attempts, complete job sets and merged-PR gates."""

from typing import Final

from .api import GitHub, require
from .models import Identity, Job, Pull, PullNumber, Run, Workflow

PRODUCER: Final = "python-package.yml"
PUBLISHER: Final = "python-publish.yml"
COMPONENTS: Final = (
    "buildx",
    "test_compose.py",
    "test_config.py",
    "test_container.py",
    "test_context.py",
    "test_image.py",
    "test_manifest.py",
    "test_network.py",
    "test_node.py",
    "test_plugin.py",
    "test_service.py",
    "test_stack.py",
    "test_swarm.py",
    "test_system.py",
    "test_task.py",
    "test_volume.py",
)
CHECKS: Final = (
    "Render docs",
    "build-linux-lint",
    "build-linux-test-other",
    "build-linux-test-coverage",
    "Compose and Buildx smoke tests",
    "Compose and Buildx ARM64 smoke tests",
    "build-windows",
    "Build tested distributions",
    *(f"build-linux-test-component ({component})" for component in COMPONENTS),
)
SEAL_JOB: Final = "Seal tested delivery"
PUBLISH_JOB: Final = "Publish tested distributions"


def identity(run: Run) -> Identity:
    """Capture an already authenticated run identity."""
    return Identity(
        repository=run.repository.full_name,
        repository_id=run.repository.id,
        workflow_id=run.workflow_id,
        workflow_path=run.path.split("@", 1)[0],
        run_id=run.id,
        attempt=run.run_attempt,
        sha=run.head_sha,
    )


def trusted_run(api: GitHub, run_id: int, workflow: str) -> Run:
    """Authenticate a successful latest attempt through a workflow's numeric ID."""
    repo = api.identity()
    expected = api.get(f"actions/workflows/{workflow}", Workflow)
    require(
        expected.path == f".github/workflows/{workflow}" and expected.state == "active",
        "workflow identity",
    )
    latest = api.get(f"actions/runs/{run_id}", Run)
    run = api.get(f"actions/runs/{run_id}/attempts/{latest.run_attempt}", Run)
    require(run == latest, "attempt changed")
    require(
        run.id == run_id
        and run.workflow_id == expected.id
        and run.path
        in {expected.path, f"{expected.path}@main", f"{expected.path}@refs/heads/main"},
        "run workflow linkage",
    )
    require(
        run.repository.id == repo.id
        and run.head_repository.id == repo.id
        and run.repository.full_name == repo.full_name
        and run.head_repository.full_name == repo.full_name
        and run.head_branch == "main",
        "untrusted run repository or branch",
    )
    require(
        run.event == ("push" if workflow == PRODUCER else "workflow_run"), "run event"
    )
    require(
        run.status == "completed" and run.conclusion == "success", "run unsuccessful"
    )
    return run


def attempt_jobs(api: GitHub, run: Run) -> tuple[Job, ...]:
    """Read all jobs of precisely this attempt, never the run's merged job view."""
    jobs = api.pages(
        f"actions/runs/{run.id}/attempts/{run.run_attempt}/jobs", Job, "jobs"
    )
    require(
        all(
            job.run_id == run.id
            and job.run_attempt in {None, run.run_attempt}
            and job.head_sha == run.head_sha
            for job in jobs
        ),
        "job attempt linkage",
    )
    require(len({job.name for job in jobs}) == len(jobs), "ambiguous jobs")
    return jobs


def successful_jobs(
    api: GitHub, run: Run, expected: tuple[str, ...]
) -> tuple[Job, ...]:
    """Reject partial reruns, skipped checks and missing matrix entries."""
    jobs = attempt_jobs(api, run)
    require({job.name for job in jobs} == set(expected), "incomplete same-attempt jobs")
    require(
        all(
            job.status == "completed"
            and job.conclusion == "success"
            and job.completed_at is not None
            for job in jobs
        ),
        "unsuccessful job",
    )
    return jobs


def merged_pull(api: GitHub, run: Run) -> None:
    """Require an authoritative merged main PR whose merge SHA is exactly tested."""
    associated = api.pages(f"commits/{run.head_sha}/pulls", PullNumber)
    for reference in associated:
        pull = api.get(f"pulls/{reference.number}", Pull)
        if (
            pull.merged
            and pull.merge_commit_sha == run.head_sha
            and pull.base.ref == "main"
            and pull.base.repo.id == api.repository_id
            and pull.base.repo.full_name == api.repository
        ):
            return
    require(False, "no exact merged main pull request")
