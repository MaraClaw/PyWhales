"""Executable delivery decisions with inert HTTP, artifact and upload boundaries.

All distribution archives and site files are synthetic. No test publishes,
dispatches, deploys, runs Docker or contacts a production service.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, BinaryIO, override

import pytest
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from scripts.delivery import __main__ as cli
from scripts.delivery import api as http
from scripts.delivery import archives, artifacts, budgets, limits, publication
from scripts.delivery.api import Curl, GitHub, Rejected, Response, Transport
from scripts.delivery.artifacts import describe, download, inventory, sha256
from scripts.delivery.chain import name, producer, project_version, receipt, seal
from scripts.delivery.distributions import inspect, smoke
from scripts.delivery.models import (
    Artifact,
    ArtifactRun,
    Artifacts,
    Base,
    GitObject,
    GitRef,
    Job,
    Jobs,
    Pull,
    PullNumber,
    Receipt,
    Release,
    Repository,
    Run,
    Seal,
    Workflow,
)
from scripts.delivery.notes import section
from scripts.delivery.pages import verify_pages_tar
from scripts.delivery.publication import confirm, prepare
from scripts.delivery.releases import release, release_record, released
from scripts.delivery.trust import (
    CHECKS,
    PUBLISH_JOB,
    SEAL_JOB,
    attempt_jobs,
    identity,
    merged_pull,
    successful_jobs,
)

ROOT = Path(__file__).resolve().parents[2]
WHEEL = "pywhales-0.6.1-py3-none-any.whl"
SDIST = "pywhales-0.6.1.tar.gz"
BASE_URL = "https://api.github.com/repos/owner/repo/"
PYPI = "https://pypi.org/pypi/pywhales/0.6.1/json"
NOTES = "### Added\n\n- Sealed release notes."
CHANGELOG = f"# Changelog\n\n## 0.6.1\n\n{NOTES}\n\n## 0.6.0\n\n- Earlier release.\n"


def zipped(files: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=compression) as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return result.getvalue()


def checkout(
    root: Path, changelog: str | bytes = CHANGELOG, version: str = "0.6.1"
) -> None:
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    data = changelog.encode() if isinstance(changelog, str) else changelog
    (root / "CHANGELOG.md").write_bytes(data)


def distributions() -> dict[str, bytes]:
    metadata = b"Name: pywhales\nVersion: 0.6.1\n"
    wheel = zipped(
        {
            "pywhales/py.typed": b"",
            "pywhales/__init__.py": b"",
            "pywhales-0.6.1.dist-info/METADATA": metadata,
        }
    )
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for filename, data in {"pywhales/py.typed": b"", "PKG-INFO": metadata}.items():
            member = tarfile.TarInfo(f"pywhales-0.6.1/{filename}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return {WHEEL: wheel, SDIST: result.getvalue()}


@dataclass
class Service(Transport):
    """Mutable in-memory external service; unknown requests fail closed."""

    routes: dict[tuple[str, str], Response] = field(default_factory=dict)
    calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    api: GitHub = field(init=False)
    source: Run = field(init=False)
    publisher: Run = field(init=False)
    jobs: tuple[Job, ...] = field(init=False)
    tested: Seal = field(init=False)
    material: dict[str, bytes] = field(default_factory=distributions)

    def __post_init__(self) -> None:
        self.api = GitHub(self, "owner/repo", 1)
        repo = Repository(id=1, full_name="owner/repo", default_branch="main")
        self.put("", repo)
        self.source = Run(
            id=100,
            run_attempt=1,
            workflow_id=10,
            path=".github/workflows/python-package.yml",
            event="push",
            head_branch="main",
            head_sha="a" * 40,
            repository=repo,
            head_repository=repo,
            status="completed",
            conclusion="success",
        )
        self.publisher = self.source.model_copy(
            update={
                "id": 200,
                "workflow_id": 20,
                "path": ".github/workflows/python-publish.yml",
                "event": "workflow_run",
                "head_sha": "b" * 40,
            }
        )
        for run in (self.source, self.publisher):
            self.put(f"actions/runs/{run.id}", run)
            self.put(f"actions/runs/{run.id}/attempts/1", run)
            self.put(
                f"actions/workflows/{Path(run.path).name}",
                Workflow(id=run.workflow_id, path=run.path, state="active"),
            )
        self.jobs = tuple(
            self.job(index + 1, title, self.source)
            for index, title in enumerate((*CHECKS, SEAL_JOB))
        )
        self.set_jobs(self.jobs)
        self.put(
            "actions/runs/200/attempts/1/jobs?per_page=100&page=1",
            Jobs(total_count=1, jobs=(self.job(99, PUBLISH_JOB, self.publisher),)),
        )
        self.put(
            f"commits/{self.source.head_sha}/pulls?per_page=100&page=1", [{"number": 7}]
        )
        self.put(
            "pulls/7",
            Pull(
                merged=True,
                merge_commit_sha=self.source.head_sha,
                base=Base(ref="main", repo=repo),
            ),
        )
        dist = self.artifact(1, "dist", self.source, self.material)
        site = self.artifact(
            2,
            "site",
            self.source,
            {"index.html": b"exact tested site", ".nojekyll": b""},
        )
        self.tested = Seal(
            producer=identity(self.source),
            jobs={job.name: job.id for job in self.jobs},
            version="0.6.1",
            notes=NOTES,
            distributions=describe(dist, self.material),
            site=describe(site, {"index.html": b"exact tested site", ".nojekyll": b""}),
        )
        seal_artifact = self.artifact(
            3,
            "seal",
            self.source,
            {"seal.json": self.tested.model_dump_json().encode()},
        )
        self.set_artifacts((dist, site, seal_artifact))
        self.routes[("GET", PYPI)] = Response(404, b"{}")
        self.put("releases?per_page=100&page=1", [])

    def put(self, path: str, value: BaseModel | JsonValue, status: int = 200) -> None:
        body = (
            value.model_dump_json().encode()
            if isinstance(value, BaseModel)
            else json.dumps(value).encode()
        )
        url = BASE_URL + path if path else BASE_URL.removesuffix("/")
        self.routes[("GET", url)] = Response(status, body)

    @override
    def request(self, method: str, url: str, body: bytes | None = None) -> Response:
        self.calls.append((method, url, body))
        return self.routes.get((method, url), Response(404, b"{}"))

    def job(self, number: int, title: str, run: Run) -> Job:
        return Job(
            id=number,
            name=title,
            run_id=run.id,
            run_attempt=1,
            head_sha=run.head_sha,
            status="completed",
            conclusion="success",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

    def artifact(
        self, number: int, kind: str, run: Run, files: dict[str, bytes]
    ) -> Artifact:
        data = zipped(files)
        self.routes[("GET", BASE_URL + f"actions/artifacts/{number}/zip")] = Response(
            200, data
        )
        return Artifact(
            id=number,
            name=name(kind, run),
            expired=False,
            digest=f"sha256:{sha256(data)}",
            created_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            workflow_run=ArtifactRun(
                id=run.id,
                repository_id=1,
                head_repository_id=1,
                head_sha=run.head_sha,
                head_branch="main",
            ),
        )

    def set_jobs(self, jobs: tuple[Job, ...]) -> None:
        self.put(
            "actions/runs/100/attempts/1/jobs?per_page=100&page=1",
            Jobs(total_count=len(jobs), jobs=jobs),
        )

    def set_artifacts(self, artifacts: tuple[Artifact, ...], run_id: int = 100) -> None:
        self.put(
            f"actions/runs/{run_id}/artifacts?per_page=100&page=1",
            Artifacts(total_count=len(artifacts), artifacts=artifacts),
        )

    def artifacts(self) -> tuple[Artifact, ...]:
        return self.api.get(
            "actions/runs/100/artifacts?per_page=100&page=1", Artifacts
        ).artifacts

    def published(self, files: dict[str, bytes] | None = None) -> None:
        material = self.material if files is None else files
        self.routes[("GET", PYPI)] = Response(
            200,
            json.dumps(
                {
                    "urls": [
                        {
                            "filename": filename,
                            "packagetype": "bdist_wheel"
                            if filename.endswith(".whl")
                            else "sdist",
                            "digests": {"sha256": sha256(data)},
                        }
                        for filename, data in material.items()
                    ]
                }
            ).encode(),
        )

    def receipted(self) -> None:
        self.published()
        document = confirm(self.api, 100)
        artifact = self.artifact(
            4,
            "receipt",
            self.publisher,
            {"receipt.json": document.model_dump_json().encode()},
        )
        self.set_artifacts((artifact,), 200)

    def tagged(self, sha: str | None = None) -> None:
        self.put(
            "git/ref/tags/v0.6.1",
            GitRef(object=GitObject(type="commit", sha=sha or self.source.head_sha)),
        )

    def released(self, sha: str | None = None, body: str | None = NOTES) -> None:
        self.tagged(sha)
        self.put(
            "releases/tags/v0.6.1",
            Release(id=1, tag_name="v0.6.1", draft=False, prerelease=False, body=body),
        )


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Service:
    instance = Service()
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REPOSITORY_ID": "1",
        "GITHUB_RUN_ID": "200",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": "b" * 40,
        "PRODUCER_ATTEMPT": "1",
        "PUBLISHER_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "Curl", lambda: instance)
    monkeypatch.setattr(publication.time, "sleep", lambda _seconds: None)
    return instance


def uploaded(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", service.source.head_sha)
    for kind, artifact in zip(("DIST", "SITE"), service.artifacts()[:2], strict=True):
        monkeypatch.setenv(f"{kind}_ARTIFACT_ID", str(artifact.id))
        monkeypatch.setenv(
            f"{kind}_ARTIFACT_DIGEST", artifact.digest.removeprefix("sha256:")
        )


def invoke(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr(sys, "argv", ["delivery", *arguments])
    return cli.main()


def test_repository_identity_uses_canonical_root(service: Service) -> None:
    assert service.api.identity() == service.source.repository
    assert service.calls == [("GET", BASE_URL.removesuffix("/"), None)]


@pytest.mark.parametrize(
    "repository",
    [
        Repository(id=2, full_name="owner/repo", default_branch="main"),
        Repository(id=1, full_name="other/repo", default_branch="main"),
        Repository(id=1, full_name="owner/repo", default_branch="other"),
    ],
)
def test_repository_identity_rejects_mismatch(
    service: Service, repository: Repository
) -> None:
    service.put("", repository)
    with pytest.raises(Rejected, match="repository identity"):
        service.api.identity()


def test_cli_lifecycle_preserves_exact_packages_and_manual_site(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    upload = tmp_path / "upload"
    assert (
        invoke(monkeypatch, "prepare", "--run-id", "100", "--output", str(upload)) == 0
    )
    assert {
        path.name: path.read_bytes() for path in upload.iterdir()
    } == service.material
    service.published()
    assert (
        invoke(
            monkeypatch,
            "confirm",
            "--run-id",
            "100",
            "--output",
            str(tmp_path / "confirmed"),
        )
        == 0
    )
    service.receipted()
    service.released()
    assert invoke(monkeypatch, "release", "--run-id", "200") == 0
    site = tmp_path / "manual-site"
    assert invoke(monkeypatch, "pages", "--run-id", "200", "--output", str(site)) == 0
    assert {path.name: path.read_bytes() for path in site.iterdir()} == {
        "index.html": b"exact tested site",
        ".nojekyll": b"",
    }
    assert all(method == "GET" for method, _url, _body in service.calls)


@pytest.mark.parametrize(
    "fault",
    ["missing", "expired", "duplicate", "digest", "content", "run", "time", "attempt"],
)
def test_rejects_unproven_artifacts(service: Service, fault: str) -> None:
    artifacts = list(service.artifacts())
    match fault:
        case "missing":
            artifacts.pop(0)
        case "expired":
            artifacts[0] = artifacts[0].model_copy(update={"expired": True})
        case "duplicate":
            artifacts.append(artifacts[0].model_copy(update={"id": 5}))
        case "digest":
            service.routes[("GET", BASE_URL + "actions/artifacts/1/zip")] = Response(
                200, b"tampered"
            )
        case "content":
            artifacts[0] = artifacts[0].model_copy(
                update={"digest": f"sha256:{'0' * 64}"}
            )
        case "run":
            artifacts[0] = artifacts[0].model_copy(
                update={
                    "workflow_run": artifacts[0].workflow_run.model_copy(
                        update={"id": 101}
                    )
                }
            )
        case "time":
            artifacts[0] = artifacts[0].model_copy(
                update={"created_at": datetime(2025, 1, 1, tzinfo=UTC)}
            )
        case "attempt":
            artifacts[0] = artifacts[0].model_copy(
                update={"name": "delivery-dist-100-2"}
            )
    service.set_artifacts(tuple(artifacts))
    with pytest.raises(Rejected):
        producer(service.api, 100)


@pytest.mark.parametrize(
    "fault", ["workflow", "path", "fork", "event", "branch", "failure", "pr", "base"]
)
def test_rejects_untrusted_runs_and_inexact_merges(
    service: Service, fault: str
) -> None:
    changes = {
        "workflow": {"workflow_id": 11},
        "path": {"path": ".github/workflows/evil.yml"},
        "fork": {
            "head_repository": service.source.repository.model_copy(update={"id": 2})
        },
        "event": {"event": "pull_request"},
        "branch": {"head_branch": "topic"},
        "failure": {"conclusion": "failure"},
    }
    if fault in changes:
        run = service.source.model_copy(update=changes[fault])
        service.put("actions/runs/100", run)
        service.put("actions/runs/100/attempts/1", run)
    else:
        service.put(
            "pulls/7",
            Pull(
                merged=True,
                merge_commit_sha="c" * 40 if fault == "pr" else service.source.head_sha,
                base=Base(
                    ref="topic" if fault == "base" else "main",
                    repo=service.source.repository,
                ),
            ),
        )
    with pytest.raises(Rejected):
        producer(service.api, 100)


@pytest.mark.parametrize("fault", ["partial", "skipped", "duplicate", "attempt", "sha"])
def test_rejects_incomplete_same_attempt_checks(service: Service, fault: str) -> None:
    jobs = list(service.jobs)
    match fault:
        case "partial":
            jobs.pop(0)
        case "skipped":
            jobs[0] = jobs[0].model_copy(update={"conclusion": "skipped"})
        case "duplicate":
            jobs.append(jobs[0])
        case "attempt":
            jobs[0] = jobs[0].model_copy(update={"run_attempt": 2})
        case "sha":
            jobs[0] = jobs[0].model_copy(update={"head_sha": "c" * 40})
    service.set_jobs(tuple(jobs))
    with pytest.raises(Rejected):
        producer(service.api, 100)


ARM_CHECK = "Compose and Buildx ARM64 smoke tests"
NOTES_CHECK = "Validate release notes"
WORKFLOW = ROOT / ".github" / "workflows" / "python-package.yml"
MODERN_COMMAND = (
    "uv run --locked pytest -vv -rs --no-runtime-skip --durations=10 "
    "tests/pywhales/components/test_docker_modern_integration.py"
)
# Written out by hand, not derived from trust.CHECKS, so renaming a job on only
# one side of the workflow/seal contract fails.
EXPECTED_CHECKS = frozenset(
    {
        "Render docs",
        "build-linux-lint",
        "build-linux-test-other",
        "build-linux-test-coverage",
        "Compose and Buildx smoke tests",
        ARM_CHECK,
        "build-windows",
        "Build tested distributions",
        NOTES_CHECK,
        *(
            f"build-linux-test-component ({component})"
            for component in (
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
        ),
    }
)


def matches(pattern: str, text: str) -> tuple[str, ...]:
    return tuple(str(item) for item in re.findall(pattern, text))


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    """Machine-read facts of one `python-package.yml` job."""

    ident: str
    text: str

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(matches(r"(?m)^    ([\w-]+):", self.text))

    @property
    def runs_on(self) -> str:
        found = re.search(r"(?m)^    runs-on: (\S+)$", self.text)
        assert found, self.ident
        return str(found.group(1))

    @property
    def needs(self) -> frozenset[str]:
        found = re.search(r"(?m)^    needs:\n((?:      - .+\n)+)", self.text)
        return frozenset(matches(r"- (\S+)", found.group(1)) if found else ())

    @property
    def names(self) -> frozenset[str]:
        """Names GitHub reports for this job, one per matrix entry."""
        found = re.search(r"(?m)^    name: (.+)$", self.text)
        title = str(found.group(1)) if found else self.ident
        matrix = re.search(
            r"(?m)^        component:\n((?:          - .+\n)+)", self.text
        )
        if matrix is None:
            return frozenset({title})
        return frozenset(
            f"{title} ({component})"
            for component in matches(r"- (\S+)", matrix.group(1))
        )

    @property
    def command(self) -> str:
        return " ".join(self.text.split())


def workflow_jobs() -> dict[str, WorkflowJob]:
    section = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
    blocks = re.split(r"(?m)^  (?=[\w-]+:$)", section)[1:]
    jobs = [WorkflowJob(block.split(":", 1)[0], block) for block in blocks]
    return {job.ident: job for job in jobs}


def test_workflow_jobs_and_seal_contract_agree_independently() -> None:
    jobs = workflow_jobs()
    seal_job = next(job for job in jobs.values() if SEAL_JOB in job.names)
    names = [name for job in jobs.values() for name in sorted(job.names)]

    assert len(names) == len(set(names))
    assert set(names) == {*EXPECTED_CHECKS, SEAL_JOB}
    assert set(CHECKS) == EXPECTED_CHECKS
    assert len(CHECKS) == len(EXPECTED_CHECKS)
    assert seal_job.needs == set(jobs) - {seal_job.ident}


def test_native_arm64_smoke_job_is_required_and_runs_the_modern_suite() -> None:
    jobs = workflow_jobs()
    arm, amd64 = jobs["build-linux-test-modern-arm64"], jobs["build-linux-test-modern"]

    assert arm.names == {ARM_CHECK}
    assert arm.runs_on == "ubuntu-24.04-arm"
    assert amd64.names == {"Compose and Buildx smoke tests"}
    assert amd64.runs_on == "ubuntu-24.04"
    assert {"if", "strategy", "continue-on-error", "container"}.isdisjoint(arm.keys)
    assert arm.keys == amd64.keys
    for job in (arm, amd64):
        assert MODERN_COMMAND in job.command
        assert "./scripts/download-docker-plugins.sh" in job.command
    assert 'test "$(uname -m)" = aarch64' in arm.command
    assert 'test "$RUNNER_ARCH" = ARM64' in arm.command
    assert "{{.Server.Arch}}')\" = arm64" in arm.command
    for emulation in ("qemu", "binfmt", "--platform", "|| true"):
        assert emulation not in arm.command.lower()
    assert {"build-linux-test-modern", "build-linux-test-modern-arm64"} <= jobs[
        "delivery-eligible"
    ].needs


def test_release_notes_job_is_a_dedicated_required_check() -> None:
    jobs = workflow_jobs()
    job, neighbor = jobs["release-notes"], jobs["build-distributions"]
    triggers = WORKFLOW.read_text().split("\njobs:\n", 1)[0]

    assert job.names == {NOTES_CHECK}
    assert re.search(rf"(?m)^    name: {re.escape(NOTES_CHECK)}$", job.text)
    assert {"if", "needs", "strategy", "continue-on-error", "container"}.isdisjoint(
        job.keys
    )
    assert "uv run --locked python -m scripts.delivery notes" in job.command
    assert set(matches(r"uses: (\S+)", job.text)) <= set(
        matches(r"uses: (\S+)", neighbor.text)
    )
    assert "release-notes" in jobs["delivery-eligible"].needs
    assert "  pull_request: {}" in triggers
    assert "  push:\n    branches:\n      - main" in triggers


def arm_faults(jobs: tuple[Job, ...], fault: str) -> tuple[Job, ...]:
    index = next(i for i, job in enumerate(jobs) if job.name == ARM_CHECK)
    arm, result = jobs[index], list(jobs)
    match fault:
        case "missing":
            del result[index]
        case "failed" | "skipped" | "cancelled":
            conclusion = {"failed": "failure"}.get(fault, fault)
            result[index] = arm.model_copy(update={"conclusion": conclusion})
        case "unfinished":
            result[index] = arm.model_copy(
                update={
                    "status": "in_progress",
                    "conclusion": None,
                    "completed_at": None,
                }
            )
        case "no-completion-time":
            result[index] = arm.model_copy(update={"completed_at": None})
        case "attempt":
            result[index] = arm.model_copy(update={"run_attempt": 2})
        case "sha":
            result[index] = arm.model_copy(update={"head_sha": "c" * 40})
        case "duplicate":
            result.append(arm.model_copy(update={"id": 500}))
        case "extra":
            result.append(
                arm.model_copy(update={"id": 500, "name": "Compose and Buildx s390x"})
            )
        case "renamed":
            result[index] = arm.model_copy(
                update={"name": "Compose and Buildx smoke tests (arm64)"}
            )
        case _:
            raise AssertionError(fault)
    return tuple(result)


@pytest.mark.parametrize("gate", ["sealer", "consumer"])
@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("missing", "incomplete"),
        ("failed", "sealer checks failed|unsuccessful job"),
        ("skipped", "sealer checks failed|unsuccessful job"),
        ("cancelled", "sealer checks failed|unsuccessful job"),
        ("unfinished", "sealer checks failed|unsuccessful job"),
        ("no-completion-time", "sealer checks failed|unsuccessful job"),
        ("attempt", "job attempt linkage"),
        ("sha", "job attempt linkage"),
        ("duplicate", "ambiguous jobs"),
        ("extra", "incomplete"),
        ("renamed", "incomplete"),
    ],
)
def test_arm64_smoke_job_must_succeed_in_this_attempt_before_sealing(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gate: str,
    fault: str,
    reason: str,
) -> None:
    checkout(tmp_path)
    uploaded(service, monkeypatch)

    def run() -> object:
        if gate == "sealer":
            return seal(service.api, 100, tmp_path)
        return producer(service.api, 100)

    assert run()
    service.set_jobs(arm_faults(service.jobs, fault))
    with pytest.raises(Rejected, match=reason):
        run()


def test_arm64_job_from_an_earlier_attempt_cannot_satisfy_a_rerun(
    service: Service,
) -> None:
    rerun = service.source.model_copy(update={"run_attempt": 2})
    jobs = tuple(job.model_copy(update={"run_attempt": 2}) for job in service.jobs)
    stale = tuple(
        job.model_copy(update={"run_attempt": 1}) if job.name == ARM_CHECK else job
        for job in jobs
    )
    expected = (*CHECKS, SEAL_JOB)

    def list_attempt_two(listing: tuple[Job, ...]) -> None:
        service.put(
            "actions/runs/100/attempts/2/jobs?per_page=100&page=1",
            Jobs(total_count=len(listing), jobs=listing),
        )

    list_attempt_two(jobs)
    assert len(successful_jobs(service.api, rerun, expected)) == len(expected)
    list_attempt_two(stale)
    with pytest.raises(Rejected, match="job attempt linkage"):
        successful_jobs(service.api, rerun, expected)
    list_attempt_two(tuple(job for job in jobs if job.name != ARM_CHECK))
    with pytest.raises(Rejected, match="incomplete same-attempt jobs"):
        successful_jobs(service.api, rerun, expected)


@pytest.mark.parametrize("existing", [(), (WHEEL,), (WHEEL, SDIST)])
def test_partial_retries_upload_only_missing_exact_bytes(
    service: Service, tmp_path: Path, existing: tuple[str, ...]
) -> None:
    service.published({filename: service.material[filename] for filename in existing})
    prepare(service.api, 100, tmp_path / "upload")
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "upload").iterdir()
    } == {
        filename: data
        for filename, data in service.material.items()
        if filename not in existing
    }


@pytest.mark.parametrize("files", [{WHEEL: b"conflict"}, {"other.whl": b"unexpected"}])
def test_destination_conflicts_stop_before_upload(
    service: Service, tmp_path: Path, files: dict[str, bytes]
) -> None:
    service.published(files)
    with pytest.raises(Rejected, match="destination"):
        prepare(service.api, 100, tmp_path / "upload")
    assert not (tmp_path / "upload").exists()


def test_skipped_publication_cannot_issue_receipt(service: Service) -> None:
    with pytest.raises(Rejected, match="incomplete"):
        confirm(service.api, 100)


def test_skipped_publisher_job_cannot_substitute_receipt(service: Service) -> None:
    service.receipted()
    job = service.job(99, PUBLISH_JOB, service.publisher).model_copy(
        update={"conclusion": "skipped"}
    )
    service.put(
        "actions/runs/200/attempts/1/jobs?per_page=100&page=1",
        Jobs(total_count=1, jobs=(job,)),
    )
    with pytest.raises(Rejected, match="unsuccessful job"):
        receipt(service.api, 200)


@pytest.mark.parametrize("operation", ["release", "pages"])
def test_automatic_and_manual_paths_reject_wrong_tag(
    service: Service, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service.receipted()
    service.released("c" * 40)
    assert invoke(monkeypatch, operation, "--run-id", "200") == 1


@pytest.mark.parametrize("race", [False, True])
def test_create_and_concurrent_release_verify_exact_final_tag(
    service: Service, race: bool
) -> None:
    service.receipted()
    document = receipt(service.api, 200)
    original = service.request

    def request(method: str, url: str, body: bytes | None = None) -> Response:
        if method == "POST" and url == BASE_URL + "git/refs":
            service.put(
                "git/ref/tags/v0.6.1",
                GitRef(object=GitObject(type="commit", sha=service.source.head_sha)),
            )
            return Response(422 if race else 201, b"{}")
        if method == "POST" and url == BASE_URL + "releases":
            assert body is not None
            assert json.loads(body) == {
                "tag_name": "v0.6.1",
                "name": "v0.6.1",
                "target_commitish": service.source.head_sha,
                "body": NOTES,
                "draft": True,
                "prerelease": False,
            }
            service.released()
            service.put(
                "releases/tags/v0.6.1",
                Release(id=1, tag_name="v0.6.1", draft=True, prerelease=False),
            )
            return Response(
                422 if race else 201,
                Release(id=1, tag_name="v0.6.1", draft=True, prerelease=False)
                .model_dump_json()
                .encode(),
            )
        if method == "PATCH" and url == BASE_URL + "releases/1":
            assert body is not None
            assert json.loads(body) == {"body": NOTES, "draft": False}
            service.released()
            return Response(200, b"{}")
        return original(method, url, body)

    class Race(Transport):
        @override
        def request(self, method: str, url: str, body: bytes | None = None) -> Response:
            return request(method, url, body)

    service.api = GitHub(Race(), "owner/repo", 1)
    release(service.api, document)


class ReleaseAPI(Transport):
    """Stateful GitHub release endpoints; other requests use the shared service."""

    def __init__(
        self,
        service: Service,
        current: Release | None = None,
        *,
        hidden: bool = False,
        stored: Callable[[str], str] = str,
    ) -> None:
        self.service = service
        self.current = current
        self.hidden = hidden
        self.stored = stored
        self.writes: list[tuple[str, dict[str, JsonValue]]] = []

    @override
    def request(self, method: str, url: str, body: bytes | None = None) -> Response:
        match method, url.removeprefix(BASE_URL):
            case "GET", "releases/tags/v0.6.1":
                if self.current and not self.current.draft and not self.hidden:
                    return Response(200, self.current.model_dump_json().encode())
                return Response(404, b"{}")
            case "GET", "releases?per_page=100&page=1":
                visible = [self.current] if self.current and not self.hidden else []
                return Response(
                    200,
                    json.dumps(
                        [item.model_dump(mode="json") for item in visible]
                    ).encode(),
                )
            case "POST", "releases":
                assert body is not None
                payload = json.loads(body)
                self.writes.append((method, payload))
                if self.current is not None:
                    self.hidden = False
                    return Response(422, b"{}")
                self.current = Release(
                    id=1,
                    tag_name=payload["tag_name"],
                    draft=payload["draft"],
                    prerelease=payload["prerelease"],
                    body=payload["body"],
                )
                return Response(201, self.current.model_dump_json().encode())
            case "PATCH", "releases/1":
                assert body is not None and self.current is not None
                payload = json.loads(body)
                self.writes.append((method, payload))
                self.current = self.current.model_copy(
                    update={
                        "draft": payload["draft"],
                        "body": self.stored(payload["body"]),
                    }
                )
                return Response(200, self.current.model_dump_json().encode())
        return self.service.request(method, url, body)

    def github(self) -> GitHub:
        return GitHub(self, "owner/repo", 1)


def draft(body: str | None = "Generated notes", *, prerelease: bool = False) -> Release:
    return Release(
        id=1, tag_name="v0.6.1", draft=True, prerelease=prerelease, body=body
    )


def sealed_release(service: Service) -> Receipt:
    service.receipted()
    service.tagged()
    return receipt(service.api, 200)


CREATED = {
    "tag_name": "v0.6.1",
    "name": "v0.6.1",
    "target_commitish": "a" * 40,
    "body": NOTES,
    "draft": True,
    "prerelease": False,
}
PUBLISHED = {"body": NOTES, "draft": False}


@pytest.mark.parametrize("start", ["absent", "raced-draft", "visible-draft"])
def test_release_publishes_sealed_notes_with_one_patch(
    service: Service, start: str
) -> None:
    document = sealed_release(service)
    fake = ReleaseAPI(
        service,
        None if start == "absent" else draft(),
        hidden=start == "raced-draft",
    )
    release(fake.github(), document)
    assert (
        fake.writes
        == {
            "absent": [("POST", CREATED), ("PATCH", PUBLISHED)],
            "raced-draft": [("POST", CREATED), ("PATCH", PUBLISHED)],
            "visible-draft": [("PATCH", PUBLISHED)],
        }[start]
    )
    assert fake.current == Release(
        id=1, tag_name="v0.6.1", draft=False, prerelease=False, body=NOTES
    )


@pytest.mark.parametrize(
    "body",
    [
        NOTES,
        NOTES.replace("\n", "\r\n") + "\r\n",
        "\n".join(f"{line}  " for line in NOTES.split("\n")) + "\n\n",
    ],
    ids=["exact", "crlf", "trailing-whitespace"],
)
def test_published_release_with_matching_body_is_not_patched(
    service: Service, body: str
) -> None:
    document = sealed_release(service)
    published = Release(
        id=1, tag_name="v0.6.1", draft=False, prerelease=False, body=body
    )
    fake = ReleaseAPI(service, published)
    release(fake.github(), document)
    assert fake.writes == []
    assert fake.current == published


@pytest.mark.parametrize(
    "body",
    [None, "", "Generated notes", NOTES + "\n\nFull Changelog: ..."],
    ids=["null", "empty", "generated", "appended"],
)
def test_published_release_with_other_body_is_rejected_untouched(
    service: Service, body: str | None
) -> None:
    document = sealed_release(service)
    published = Release(
        id=1, tag_name="v0.6.1", draft=False, prerelease=False, body=body
    )
    fake = ReleaseAPI(service, published)
    with pytest.raises(Rejected, match="release body mismatch"):
        release(fake.github(), document)
    assert fake.writes == []
    assert fake.current == published


@pytest.mark.parametrize(
    ("fault", "reason"),
    [("prerelease", "release state"), ("tag", "tag commit mismatch")],
)
def test_draft_is_not_patched_before_tag_and_state_checks(
    service: Service, fault: str, reason: str
) -> None:
    document = sealed_release(service)
    fake = ReleaseAPI(service, draft(prerelease=fault == "prerelease"))
    if fault == "tag":
        service.tagged("c" * 40)
    with pytest.raises(Rejected, match=reason):
        release(fake.github(), document)
    assert fake.writes == []


@pytest.mark.parametrize("start", ["absent", "visible-draft"])
def test_read_back_rejects_a_body_that_differs_after_publication(
    service: Service, start: str
) -> None:
    document = sealed_release(service)
    fake = ReleaseAPI(
        service,
        None if start == "absent" else draft(),
        stored=lambda body: body + "\n\nGenerated by GitHub",
    )
    with pytest.raises(Rejected, match="release body mismatch"):
        release(fake.github(), document)
    assert [method for method, _payload in fake.writes].count("PATCH") == 1


def test_read_back_accepts_line_ending_and_trailing_whitespace_drift(
    service: Service,
) -> None:
    document = sealed_release(service)
    fake = ReleaseAPI(
        service, draft(), stored=lambda body: body.replace("\n", "\r\n") + "  \r\n"
    )
    release(fake.github(), document)
    assert fake.writes == [("PATCH", PUBLISHED)]


@pytest.mark.parametrize("body", [None, "", "Hand-edited notes"])
def test_released_never_compares_release_bodies(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str | None
) -> None:
    document = sealed_release(service)
    service.released(body=body)
    released(service.api, document)
    site = tmp_path / "site"
    assert invoke(monkeypatch, "pages", "--run-id", "200", "--output", str(site)) == 0


def test_annotated_tag_is_resolved_not_trusted_by_name(service: Service) -> None:
    service.receipted()
    service.released()
    service.put(
        "git/ref/tags/v0.6.1", GitRef(object=GitObject(type="tag", sha="c" * 40))
    )
    service.put(
        f"git/tags/{'c' * 40}",
        GitRef(object=GitObject(type="commit", sha=service.source.head_sha)),
    )
    release(service.api, receipt(service.api, 200))


def test_trigger_attempt_must_match_even_with_valid_material(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRODUCER_ATTEMPT", "2")
    assert (
        invoke(
            monkeypatch,
            "prepare",
            "--run-id",
            "100",
            "--output",
            str(tmp_path / "upload"),
        )
        == 1
    )
    assert not (tmp_path / "upload").exists()


def test_sealer_partial_rerun_reports_missing_current_attempt_artifact(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uploaded(service, monkeypatch)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    run = service.source.model_copy(update={"run_attempt": 3})
    service.put("actions/runs/100/attempts/3", run)
    jobs = tuple(job.model_copy(update={"run_attempt": 3}) for job in service.jobs)
    service.put(
        "actions/runs/100/attempts/3/jobs?per_page=100&page=1",
        Jobs(total_count=len(jobs), jobs=jobs),
    )
    output = tmp_path / "delivery-seal"
    assert invoke(monkeypatch, "seal", "--run-id", "100", "--output", str(output)) == 1
    error = capsys.readouterr().err
    assert "delivery-dist-100-3" in error
    assert "Re-run all jobs" in error
    assert not output.exists()
    assert not any("/zip" in url for _method, url, _body in service.calls)


def test_sealer_checks_actual_uploader_ids_and_digests(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout(tmp_path)
    uploaded(service, monkeypatch)
    assert seal(service.api, 100, tmp_path) == service.tested
    monkeypatch.setenv("DIST_ARTIFACT_ID", "987")
    with pytest.raises(Rejected, match="uploader output"):
        seal(service.api, 100, tmp_path)


@pytest.mark.parametrize(
    ("changelog", "reason"),
    [
        ("# Changelog\n\n## 0.6.0\n\n- Earlier release.\n", "missing"),
        (
            "## 0.6.10\n\n- a\n\n## 0.6.1 (draft)\n\n- b\n\n### 0.6.1\n\n- c\n\n"
            "##0.6.1\n- d\n\n## 0.6x1\n\n- e\n",
            "missing",
        ),
        ("## 0.6.1\n\n   \n\n## 0.6.0\n\n- Earlier release.\n", "empty"),
        ("## 0.6.1\n", "empty"),
        (CHANGELOG + "\n## 0.6.1\n\n- Again.\n", "duplicated"),
    ],
    ids=["absent", "near-misses", "empty", "empty-at-end", "duplicated"],
)
def test_sealer_rejects_missing_empty_and_duplicated_notes(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changelog: str,
    reason: str,
) -> None:
    checkout(tmp_path, changelog)
    uploaded(service, monkeypatch)
    with pytest.raises(Rejected, match=f"release notes section {reason}"):
        seal(service.api, 100, tmp_path)


def test_sealer_extracts_exact_section_from_crlf_changelog(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = (
        "# Changelog\r\n\r\n## 0.6.10\r\n\r\n- Decoy.\r\n\r\n## 0.6.1\r\n\r\n\r\n"
        "### Added\r\n\r\n- One.\r\n\r\n#### Detail\r\n\r\n### Fixed\r\n\r\n- Two.\r\n"
        "\r\n\r\n## Unreleased\r\n\r\n- Later.\r\n"
    )
    checkout(tmp_path, changelog)
    uploaded(service, monkeypatch)
    assert seal(service.api, 100, tmp_path).notes == (
        "### Added\n\n- One.\n\n#### Detail\n\n### Fixed\n\n- Two."
    )


def test_sealer_rejects_changelog_that_is_not_utf8(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout(tmp_path, b"## 0.6.1\n\n- Caf\xe9.\n")
    uploaded(service, monkeypatch)
    with pytest.raises(Rejected, match="not UTF-8"):
        seal(service.api, 100, tmp_path)


def test_sealer_requires_a_changelog_file(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout(tmp_path)
    (tmp_path / "CHANGELOG.md").unlink()
    uploaded(service, monkeypatch)
    with pytest.raises(FileNotFoundError):
        seal(service.api, 100, tmp_path)


@pytest.mark.parametrize("excess", [False, True])
def test_sealer_bounds_notes_in_utf8_bytes_not_characters(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excess: bool,
) -> None:
    notes = "é" * 10
    checkout(tmp_path, f"## 0.6.1\n\n{notes}\n")
    uploaded(service, monkeypatch)
    monkeypatch.setattr(limits, "NOTES_BYTES", len(notes.encode()) - int(excess))
    if excess:
        with pytest.raises(Rejected, match="release notes size budget"):
            seal(service.api, 100, tmp_path)
    else:
        assert seal(service.api, 100, tmp_path).notes == notes


@pytest.mark.parametrize("excess", [False, True])
def test_sealer_independently_bounds_changelog_file(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excess: bool,
) -> None:
    checkout(tmp_path)
    monkeypatch.setattr(
        limits, "CHANGELOG_BYTES", len(CHANGELOG.encode()) - int(excess)
    )
    uploaded(service, monkeypatch)
    if excess:
        with pytest.raises(Rejected, match="input byte budget exceeded"):
            seal(service.api, 100, tmp_path)
    else:
        assert seal(service.api, 100, tmp_path) == service.tested


def test_notes_budget_stays_inside_github_body_limit() -> None:
    assert 0 < limits.NOTES_BYTES <= 125_000


@pytest.mark.parametrize(
    ("heading", "found"),
    [
        ("## 0.7.0rc1", True),
        ("## 0.7.0rc10", False),
        ("## 0.7.0rc1 (draft)", False),
        ("## 0.7.0rc1 ", False),
        ("### 0.7.0rc1", False),
        ("##0.7.0rc1", False),
        ("## 0.7x0rc1", False),
    ],
)
def test_notes_heading_is_matched_exactly_without_patterns(
    heading: str, found: bool
) -> None:
    text = f"# Changelog\n\n{heading}\n\n- Body.\n"
    if found:
        assert section(text, "0.7.0rc1") == "- Body."
    else:
        with pytest.raises(Rejected, match="section missing"):
            section(text, "0.7.0rc1")


def entry(body: str, tail: str = "") -> str:
    return (
        f"# Changelog\n\n## 0.6.1\n\n{body}\n{tail}\n## 0.6.0\n\n- Earlier release.\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "```md\n## 0.6.1\n## Other\n```\n\n- Tail.",
        "~~~md\n## 0.6.1\n## Other\n~~~\n\n- Tail.",
        "````\n```\n## Fake\n`````\n\n- Tail.",
        "~~~~\n~~~\n## Fake\n~~~~~~\n\n- Tail.",
        "   ```\n## Fake\n   ```\n\n- Tail.",
        "```\n## Fake\n```  \t\n\n- Tail.",
        "```\n~~~\n## Fake\n```\n\n- Tail.",
        "```\n```python\n## Fake\n```\n\n- Tail.",
        "```\n    ```\n## Fake\n```\n\n- Tail.",
        "~~~a`b~c\n## Fake\n~~~\n\n- Tail.",
    ],
    ids=[
        "backtick",
        "tilde",
        "longer-closer",
        "longer-tilde-closer",
        "indented-three",
        "closer-blanks",
        "other-character",
        "closer-info",
        "indented-four-closer",
        "tilde-info-backtick",
    ],
)
def test_notes_fenced_headings_never_end_the_section(body: str) -> None:
    assert section(entry(body), "0.6.1") == body


@pytest.mark.parametrize(
    "body",
    [
        "    ```\n- Body.",
        "\t```\n- Body.",
        "```a`b\n- Body.",
        "``\n- Body.",
    ],
    ids=["four-spaces", "tab", "backtick-in-info", "two-backticks"],
)
def test_notes_non_fences_do_not_hide_the_next_heading(body: str) -> None:
    assert section(entry(body), "0.6.1") == body


def test_notes_fenced_fake_heading_is_not_a_duplicate_or_the_section() -> None:
    fake = "```md\n## 0.6.1\n\n- Fake.\n```\n"
    assert section(f"{fake}\n## 0.6.1\n\n- Real.\n", "0.6.1") == "- Real."
    with pytest.raises(Rejected, match="section missing"):
        section(f"{fake}\n## 0.6.0\n\n- Old.\n", "0.6.1")


@pytest.mark.parametrize(
    "text",
    [
        "## 0.6.1\n\n```\n- Body.\n",
        "## 0.6.1\n\n- Body.\n\n## 0.6.0\n\n```\n- Old.\n",
        "```\n## 0.6.1\n\n- Body.\n",
        "````\n```\n\n## 0.6.1\n\n- Body.\n",
        "## 0.6.1\n\n```\n    ```\n- Body.\n",
        "## 0.6.1\n\n```\n``` x\n- Body.\n",
        "## 0.6.1\n\n~~~\n```\n- Body.\n",
        "## 0.6.0\n\n```\n- Old.\n",
    ],
    ids=[
        "in-section",
        "older-section",
        "before-heading",
        "shorter-closer",
        "indented-closer",
        "closer-with-text",
        "other-character",
        "version-missing",
    ],
)
def test_notes_unclosed_fence_anywhere_is_rejected_with_its_own_reason(
    text: str,
) -> None:
    with pytest.raises(Rejected, match="release notes fence unclosed"):
        section(text, "0.6.1")


def test_sealer_keeps_fenced_example_in_notes(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "```md\n## 0.6.1\n```\n\n- Example."
    checkout(tmp_path, entry(body))
    uploaded(service, monkeypatch)
    assert seal(service.api, 100, tmp_path).notes == body


@pytest.mark.parametrize(
    ("changes", "removed"),
    [
        ({"schema_version": 1}, ()),
        ({"schema_version": 1}, ("notes",)),
        ({}, ("notes",)),
        ({"notes": ""}, ()),
        ({"notes": " \n"}, ()),
        ({"notes": "x" * (limits.NOTES_BYTES + 1)}, ()),
    ],
)
def test_seal_documents_require_schema_version_2_and_bounded_notes(
    service: Service, changes: dict[str, JsonValue], removed: tuple[str, ...]
) -> None:
    data = json.loads(service.tested.model_dump_json()) | changes
    for key in removed:
        del data[key]
    with pytest.raises(ValidationError):
        Seal.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("nested", [False, True])
def test_receipt_documents_reject_schema_version_1(
    service: Service, nested: bool
) -> None:
    service.receipted()
    data = json.loads(confirm(service.api, 100).model_dump_json())
    assert data["schema_version"] == 2 and data["seal"]["schema_version"] == 2
    if nested:
        data["seal"]["schema_version"] = 1
    else:
        data["schema_version"] = 1
    with pytest.raises(ValidationError):
        Receipt.model_validate_json(json.dumps(data))


def test_consumers_reject_version_1_seal_and_receipt_artifacts(
    service: Service,
) -> None:
    service.receipted()
    document = confirm(service.api, 100)
    old_receipt = json.loads(document.model_dump_json()) | {"schema_version": 1}
    service.set_artifacts(
        (
            service.artifact(
                4,
                "receipt",
                service.publisher,
                {"receipt.json": json.dumps(old_receipt).encode()},
            ),
        ),
        200,
    )
    with pytest.raises(ValidationError):
        receipt(service.api, 200)

    old_seal = json.loads(service.tested.model_dump_json()) | {"schema_version": 1}
    dist, site, _seal = service.artifacts()
    service.set_artifacts(
        (
            dist,
            site,
            service.artifact(
                3, "seal", service.source, {"seal.json": json.dumps(old_seal).encode()}
            ),
        )
    )
    with pytest.raises(ValidationError):
        producer(service.api, 100)


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "foo/../../escape", "a\\b", ".github/workflows/x"],
)
def test_archive_paths_never_escape_or_supply_executable_checkout(
    service: Service, path: str
) -> None:
    artifact = service.artifact(55, "site", service.source, {path: b"untrusted"})
    with pytest.raises(Rejected, match="unsafe archive"):
        download(service.api, artifact)


def github_free_environment() -> dict[str, str]:
    kept = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITHUB_", "GH_"))
    }
    return kept | {"PYTHONPATH": str(ROOT)}


@pytest.mark.parametrize(
    ("changelog", "code", "message"),
    [
        (entry("```md\n## 0.6.1\n```\n\n- Example."), 0, ""),
        ("## 0.6.1\n\n```\n- Body.\n", 1, "release notes fence unclosed"),
        ("## 0.6.0\n\n- Earlier release.\n", 1, "release notes section missing"),
    ],
    ids=["valid", "unclosed", "missing"],
)
def test_notes_operation_needs_no_github_environment(
    tmp_path: Path, changelog: str, code: int, message: str
) -> None:
    checkout(tmp_path, changelog)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.delivery", "notes"],
        cwd=tmp_path,
        env=github_free_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == code, result.stderr
    assert result.stdout == ""
    assert result.stderr.strip() == (f"delivery rejected: {message}" if message else "")


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("", ""),
        ("size", "input byte budget exceeded"),
        ("encoding", "changelog is not UTF-8"),
    ],
)
def test_notes_operation_keeps_the_seal_input_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
    message: str,
) -> None:
    def no_github() -> Transport:
        raise AssertionError("notes must not reach GitHub")

    monkeypatch.setattr(cli, "Curl", no_github)
    monkeypatch.chdir(tmp_path)
    for key in ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    checkout(
        tmp_path, b"## 0.6.1\n\n- Caf\xe9.\n" if fault == "encoding" else CHANGELOG
    )
    if fault == "size":
        monkeypatch.setattr(limits, "CHANGELOG_BYTES", len(CHANGELOG.encode()) - 1)
    assert invoke(monkeypatch, "notes") == (1 if fault else 0)
    assert message in capsys.readouterr().err


def test_seal_and_notes_share_the_project_version_reader(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout(tmp_path)
    assert project_version(tmp_path) == "0.6.1"
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    uploaded(service, monkeypatch)
    with pytest.raises(Rejected, match="project version missing"):
        seal(service.api, 100, tmp_path)
    monkeypatch.chdir(tmp_path)
    assert invoke(monkeypatch, "notes") == 1
    assert "delivery rejected: project version missing" in capsys.readouterr().err


def test_real_module_cli_help_and_invalid_input() -> None:
    for arguments, expected in (
        (["--help"], 0),
        (["pages", "--run-id", "not-a-number"], 2),
        (["pages", "--run-id", "0"], 1),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.delivery", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == expected, result.stderr
        if arguments == ["--help"]:
            assert "notes" in result.stdout


def test_file_hashes_reject_repacked_tampering_with_valid_archive_digest(
    service: Service,
) -> None:
    artifacts = list(service.artifacts())
    artifacts[1] = service.artifact(
        2, "site", service.source, {"index.html": b"changed site"}
    )
    service.set_artifacts(tuple(artifacts))
    with pytest.raises(Rejected, match="content or identity"):
        producer(service.api, 100)


@pytest.mark.parametrize(
    "fault", ["missing", "expired", "duplicate", "tamper", "identity"]
)
def test_receipt_material_is_not_a_successful_run_substitute(
    service: Service, fault: str
) -> None:
    service.receipted()
    artifacts = list(
        service.api.get(
            "actions/runs/200/artifacts?per_page=100&page=1", Artifacts
        ).artifacts
    )
    match fault:
        case "missing":
            artifacts.clear()
        case "expired":
            artifacts[0] = artifacts[0].model_copy(update={"expired": True})
        case "duplicate":
            artifacts.append(artifacts[0].model_copy(update={"id": 5}))
        case "tamper":
            service.routes[("GET", BASE_URL + "actions/artifacts/4/zip")] = Response(
                200, b"tampered receipt"
            )
        case "identity":
            document = confirm(service.api, 100)
            document = document.model_copy(
                update={
                    "publisher": document.publisher.model_copy(update={"attempt": 2})
                }
            )
            artifacts[0] = service.artifact(
                4,
                "receipt",
                service.publisher,
                {"receipt.json": document.model_dump_json().encode()},
            )
    service.set_artifacts(tuple(artifacts), 200)
    with pytest.raises(Rejected):
        receipt(service.api, 200)


@pytest.mark.parametrize("status", [403, 404, 410, 500])
def test_download_boundary_failures_cannot_publish(
    service: Service, status: int
) -> None:
    service.routes[("GET", BASE_URL + "actions/artifacts/1/zip")] = Response(
        status, b"unavailable"
    )
    with pytest.raises(Rejected, match="download failed"):
        producer(service.api, 100)


@pytest.mark.parametrize("fault", ["kind", "duplicate", "incomplete"])
def test_destination_confirmation_requires_both_unique_correct_kinds(
    service: Service, fault: str
) -> None:
    service.published()
    if fault == "incomplete":
        service.published({WHEEL: service.material[WHEEL]})
    else:
        release_data = {
            "urls": [
                {
                    "filename": WHEEL,
                    "packagetype": "sdist" if fault == "kind" else "bdist_wheel",
                    "digests": {"sha256": sha256(service.material[WHEEL])},
                },
                {
                    "filename": SDIST if fault == "kind" else WHEEL,
                    "packagetype": "sdist" if fault == "kind" else "bdist_wheel",
                    "digests": {
                        "sha256": sha256(
                            service.material[SDIST if fault == "kind" else WHEEL]
                        )
                    },
                },
            ]
        }
        service.routes[("GET", PYPI)] = Response(200, json.dumps(release_data).encode())
    with pytest.raises(Rejected):
        confirm(service.api, 100)


def test_confirm_rejects_triggering_attempt_mismatch_before_polling(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCER_ATTEMPT", "2")
    sleeps: list[float] = []
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)
    with pytest.raises(Rejected, match="triggering producer attempt mismatch"):
        confirm(service.api, 100)
    assert sleeps == []


def test_confirm_polls_until_missing_inventory_becomes_complete(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        service.published()

    monkeypatch.setattr(publication.time, "sleep", fake_sleep)
    document = confirm(service.api, 100)
    assert document.confirmed is True
    assert sleeps == [publication.CONFIRM_RETRY_DELAY_SECONDS]


def test_confirm_polls_through_partial_before_complete(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            service.published({WHEEL: service.material[WHEEL]})
        else:
            service.published()

    monkeypatch.setattr(publication.time, "sleep", fake_sleep)
    document = confirm(service.api, 100)
    assert document.confirmed is True
    assert sleeps == [publication.CONFIRM_RETRY_DELAY_SECONDS] * 2


def test_confirm_exhausts_bounded_retries_with_filenames_when_still_missing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)
    with pytest.raises(Rejected, match="destination publication incomplete") as error:
        confirm(service.api, 100)
    assert (
        sleeps
        == [publication.CONFIRM_RETRY_DELAY_SECONDS] * publication.CONFIRM_RETRIES
    )
    assert WHEEL in str(error.value)
    assert SDIST in str(error.value)


def test_confirm_fails_immediately_on_conflict_after_initial_missing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        service.published({WHEEL: b"conflict"})

    monkeypatch.setattr(publication.time, "sleep", fake_sleep)
    with pytest.raises(Rejected, match="destination content mismatch"):
        confirm(service.api, 100)
    assert sleeps == [publication.CONFIRM_RETRY_DELAY_SECONDS]


def test_confirm_fails_immediately_on_invalid_http_after_initial_missing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        service.routes[("GET", PYPI)] = Response(500, b"boom")

    monkeypatch.setattr(publication.time, "sleep", fake_sleep)
    with pytest.raises(Rejected, match="pypi response"):
        confirm(service.api, 100)
    assert sleeps == [publication.CONFIRM_RETRY_DELAY_SECONDS]


def test_confirm_fails_immediately_on_malformed_response_after_initial_missing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        service.routes[("GET", PYPI)] = Response(200, b"not json")

    monkeypatch.setattr(publication.time, "sleep", fake_sleep)
    with pytest.raises(ValidationError):
        confirm(service.api, 100)
    assert sleeps == [publication.CONFIRM_RETRY_DELAY_SECONDS]


def test_cli_confirm_succeeds_after_missing_inventory_becomes_complete(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publication.time, "sleep", lambda _seconds: service.published())
    output = tmp_path / "confirmed"
    assert (
        invoke(monkeypatch, "confirm", "--run-id", "100", "--output", str(output)) == 0
    )
    assert json.loads((output / "receipt.json").read_text())["confirmed"] is True


def test_cli_confirm_exits_nonzero_after_bounded_exhaustion(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(publication.time, "sleep", sleeps.append)
    output = tmp_path / "confirmed"
    assert (
        invoke(monkeypatch, "confirm", "--run-id", "100", "--output", str(output)) == 1
    )
    assert (
        sleeps
        == [publication.CONFIRM_RETRY_DELAY_SECONDS] * publication.CONFIRM_RETRIES
    )
    assert not output.exists()


def test_late_page_duplicate_is_rejected_instead_of_selecting_first(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "ARTIFACT_ITEMS", 200)
    artifacts = service.artifacts()
    first = tuple(
        artifacts[0].model_copy(update={"id": 1000 + index}) for index in range(100)
    )
    total = len(first) + len(artifacts)
    service.put(
        "actions/runs/100/artifacts?per_page=100&page=1",
        Artifacts(total_count=total, artifacts=first),
    )
    service.put(
        "actions/runs/100/artifacts?per_page=100&page=2",
        Artifacts(total_count=total, artifacts=artifacts),
    )
    with pytest.raises(Rejected, match="ambiguous artifact"):
        producer(service.api, 100)
    assert any(
        url.endswith("artifacts?per_page=100&page=2")
        for _method, url, _body in service.calls
    )


@pytest.mark.parametrize("operation", ["release", "pages"])
def test_manual_and_automatic_paths_cannot_use_producer_run_id(
    service: Service, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    assert invoke(monkeypatch, operation, "--run-id", "100") == 1


@pytest.mark.parametrize("fault", ["metadata", "typed", "legacy"])
@pytest.mark.parametrize("artifact", [WHEEL, SDIST])
def test_distribution_checks_read_contents_not_filenames(
    service: Service, fault: str, artifact: str
) -> None:
    files = dict(service.material)
    contents = {
        "pywhales/py.typed": b"",
        "pywhales-0.6.1.dist-info/METADATA": b"Name: pywhales\nVersion: 0.6.1\n",
    }
    match fault:
        case "metadata":
            contents["pywhales-0.6.1.dist-info/METADATA"] = (
                b"Name: pywhales\nVersion: 0.6.2\n"
            )
        case "typed":
            del contents["pywhales/py.typed"]
        case "legacy":
            contents["python_on_whales/__init__.py"] = b""
    if artifact == WHEEL:
        files[artifact] = zipped(contents)
    else:
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w:gz") as archive:
            for filename, data in contents.items():
                path = "PKG-INFO" if filename.endswith("/METADATA") else filename
                member = tarfile.TarInfo(f"pywhales-0.6.1/{path}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        files[artifact] = result.getvalue()
    with pytest.raises(Rejected):
        inspect(files, "0.6.1")


@pytest.mark.parametrize("build_sidecar", [False, True])
def test_smoke_uses_separate_isolated_installs_outside_source(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_sidecar: bool,
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.6.1"\n')
    (tmp_path / "dist").mkdir()
    for filename, data in service.material.items():
        (tmp_path / "dist" / filename).write_bytes(data)
    if build_sidecar:
        (tmp_path / "dist" / ".gitignore").write_text("*\n")
    calls: list[tuple[list[str], str]] = []

    def install(
        command: list[str], *, cwd: str, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert check and timeout > 0
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", install)
    smoke(tmp_path)
    assert {
        Path(command[command.index("--with") + 1]).name for command, _cwd in calls
    } == {WHEEL, SDIST}
    assert all(
        "-I" in command
        and "--isolated" in command
        and "--no-project" in command
        and not Path(cwd).is_relative_to(tmp_path)
        for command, cwd in calls
    )


@pytest.mark.parametrize(
    "extra", ["unexpected.txt", ".unexpected", "pywhales-0.6.0.tar.gz"]
)
def test_smoke_rejects_unexpected_build_files(tmp_path: Path, extra: str) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.6.1"\n')
    (tmp_path / "dist").mkdir()
    for filename, data in distributions().items():
        (tmp_path / "dist" / filename).write_bytes(data)
    (tmp_path / "dist" / extra).write_bytes(b"unexpected")

    with pytest.raises(Rejected, match="distribution filenames"):
        smoke(tmp_path)


def test_distribution_inspection_rejects_build_sidecar() -> None:
    files = distributions() | {".gitignore": b"*\n"}

    with pytest.raises(Rejected, match="distribution filenames"):
        inspect(files, "0.6.1")


@pytest.mark.parametrize("checkout_import", [False, True])
def test_smoke_checks_actual_uv_installs_with_cache_backed_module_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_import: bool
) -> None:
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    record = tmp_path / "imports.jsonl"
    monkeypatch.setenv("DELIVERY_SMOKE_RECORD", str(record))
    package = (
        b"import json,os,pathlib,sys\n"
        b"with open(os.environ['DELIVERY_SMOKE_RECORD'], 'a') as record:\n"
        b" record.write(json.dumps({'prefix':sys.prefix,'module':str(pathlib.Path(__file__).resolve())})+'\\n')\n"
        b"class DockerClient: pass\n"
    )
    contents = {
        "pywhales/__init__.py": package,
        "pywhales/py.typed": b"",
        "pywhales-0.6.1.dist-info/METADATA": b"Metadata-Version: 2.1\nName: pywhales\nVersion: 0.6.1\n",
        "pywhales-0.6.1.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if checkout_import:
        (tmp_path / "pywhales").mkdir()
        (tmp_path / "pywhales" / "__init__.py").write_bytes(package)
        contents["checkout.pth"] = (
            f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n"
        ).encode()
    contents["pywhales-0.6.1.dist-info/RECORD"] = "".join(
        f"{filename},,\n" for filename in contents
    ).encode()
    wheel = zipped(contents)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / WHEEL).write_bytes(wheel)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.6.1"\n')
    backend = (
        "import base64,pathlib\n"
        "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
        f" pathlib.Path(wheel_directory, {WHEEL!r}).write_bytes(base64.b64decode({base64.b64encode(wheel)!r}))\n"
        f" return {WHEEL!r}\n"
    ).encode()
    with tarfile.open(tmp_path / "dist" / SDIST, mode="w:gz") as archive:
        for filename, data in {
            "pywhales/__init__.py": package,
            "pywhales/py.typed": b"",
            "PKG-INFO": contents["pywhales-0.6.1.dist-info/METADATA"],
            "pyproject.toml": b'[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n',
            "backend.py": backend,
        }.items():
            member = tarfile.TarInfo(f"pywhales-0.6.1/{filename}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

    if checkout_import:
        with pytest.raises(subprocess.CalledProcessError):
            smoke(tmp_path)
        imported = [json.loads(line) for line in record.read_text().splitlines()]
        assert len(imported) == 1
        assert Path(imported[0]["module"]).is_relative_to(tmp_path.resolve())
    else:
        smoke(tmp_path)
        imported = [json.loads(line) for line in record.read_text().splitlines()]
        assert len(imported) == 2
        assert all(
            not Path(item["module"]).is_relative_to(Path(item["prefix"]).resolve())
            and not Path(item["module"]).is_relative_to(tmp_path.resolve())
            for item in imported
        )


@pytest.mark.parametrize("suffix", ["@main", "@refs/heads/main"])
def test_documented_run_path_ref_forms_remain_authoritatively_bound(
    service: Service, suffix: str
) -> None:
    for run in (service.source, service.publisher):
        scoped = run.model_copy(update={"path": run.path + suffix})
        service.put(f"actions/runs/{run.id}", scoped)
        service.put(f"actions/runs/{run.id}/attempts/1", scoped)
    service.receipted()
    assert receipt(service.api, 200).seal == service.tested


def test_actual_pages_tar_preserves_hidden_files_and_all_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPYFILE_DISABLE", "1")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_bytes(b"exact tested site")
    (site / ".well-known").mkdir()
    (site / ".well-known" / "record").write_bytes(b"hidden but tested")
    archive = tmp_path / "artifact.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(site), "."], check=True, timeout=10
    )
    verify_pages_tar(site, archive)
    (site / ".well-known" / "record").write_bytes(b"changed")
    with pytest.raises(Rejected, match="differs from tested site"):
        verify_pages_tar(site, archive)


def test_tag_mutation_during_creation_stays_draft_and_rejects(service: Service) -> None:
    service.receipted()
    document = receipt(service.api, 200)
    service.put(
        "git/ref/tags/v0.6.1",
        GitRef(object=GitObject(type="commit", sha=service.source.head_sha)),
    )
    original = service.request
    writes: list[str] = []

    class TagMutation(Transport):
        @override
        def request(self, method: str, url: str, body: bytes | None = None) -> Response:
            if method == "POST" and url == BASE_URL + "releases":
                assert body is not None and json.loads(body)["draft"] is True
                writes.append(method)
                service.released("c" * 40)
                service.put(
                    "releases/tags/v0.6.1",
                    Release(id=1, tag_name="v0.6.1", draft=True, prerelease=False),
                )
                return Response(
                    201,
                    Release(id=1, tag_name="v0.6.1", draft=True, prerelease=False)
                    .model_dump_json()
                    .encode(),
                )
            if method == "PATCH":
                writes.append(method)
            return original(method, url, body)

    with pytest.raises(Rejected, match="tag commit mismatch"):
        release(GitHub(TagMutation(), "owner/repo", 1), document)
    assert writes == ["POST"]


@pytest.mark.parametrize("timeout", [False, True])
def test_http_transport_failures_never_expose_authorization(
    monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fixture-secret-token")

    script = "import sys; sys.stderr.write('fixture-secret-token'); sys.exit(7)"
    processes, _commands = controlled_curl(monkeypatch, script)
    if timeout:
        clock = iter((0.0, 131.0))
        monkeypatch.setattr(http.time, "monotonic", lambda: next(clock))
    message = "HTTP transport timed out" if timeout else "HTTP transport failed"
    with pytest.raises(Rejected, match=message) as rejected:
        Curl().request("GET", BASE_URL + "actions/runs/100")
    assert "fixture-secret-token" not in str(rejected.value)
    assert processes[0].returncode is not None
    assert processes[0].stdout is not None and processes[0].stdout.closed


def controlled_curl(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> tuple[list[subprocess.Popen[bytes]], list[list[str]]]:
    processes: list[subprocess.Popen[bytes]] = []
    commands: list[list[str]] = []
    launch = subprocess.Popen

    def start(
        command: list[str], *, stdin: BinaryIO, stdout: int, stderr: int
    ) -> subprocess.Popen[bytes]:
        assert stdout == subprocess.PIPE and stderr == subprocess.DEVNULL
        commands.append(command)
        process = launch(
            [sys.executable, "-c", script], stdin=stdin, stdout=stdout, stderr=stderr
        )
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    return processes, commands


@pytest.mark.parametrize("framing", ["unknown", "dishonest"])
@pytest.mark.parametrize("size", [0, 32, 33])
def test_http_counts_received_body_without_trusting_length(
    monkeypatch: pytest.MonkeyPatch, framing: str, size: int
) -> None:
    payload = b"x" * size

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(206)
            if framing == "dishonest":
                self.send_header("Content-Length", "1")
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if framing == "dishonest":
                if payload:
                    self.wfile.write(
                        f"{len(payload):x}\r\n".encode() + payload + b"\r\n"
                    )
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(payload)

        @override
        def log_message(self, format: str, *args: str | int) -> None:
            return

    launch = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def start(
            command: list[str], *, stdin: BinaryIO, stdout: int, stderr: int
        ) -> subprocess.Popen[bytes]:
            assert command[command.index("--proto") + 1] == "=https"
            assert command[command.index("--proto-redir") + 1] == "=https"
            assert command[command.index("--max-time") + 1] == "120"
            assert command[command.index("--connect-timeout") + 1] == "10"
            local_command = list(command)
            local_command[local_command.index("--proto") + 1] = "=http"
            local_command[-1] = f"http://127.0.0.1:{server.server_port}/"
            process = launch(local_command, stdin=stdin, stdout=stdout, stderr=stderr)
            processes.append(process)
            return process

        monkeypatch.setattr(subprocess, "Popen", start)
        monkeypatch.setattr(limits, "RESPONSE_BYTES", 32)
        try:
            if size > 32:
                with pytest.raises(Rejected, match="byte budget"):
                    Curl().request("GET", "https://delivery.invalid/")
            else:
                assert Curl().request("GET", "https://delivery.invalid/") == Response(
                    206, payload
                )
        finally:
            server.shutdown()
            worker.join(timeout=5)
        assert not worker.is_alive()
    assert processes[0].returncode is not None
    assert processes[0].stdout is not None and processes[0].stdout.closed


def test_curl_discards_stderr_and_preserves_binary_body_status_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"\x00\n404\n"
    monkeypatch.setenv("GH_TOKEN", "fixture-secret-token")
    monkeypatch.setattr(limits, "RESPONSE_BYTES", len(payload))
    processes, commands = controlled_curl(
        monkeypatch,
        "import sys; sys.stderr.buffer.write(b'e'*2097152); "
        "sys.stdout.buffer.write(sys.stdin.buffer.read()+b'\\n201')",
    )
    for url in (
        BASE_URL,
        "https://pypi.org/fixture",
        "https://api.github.com.evil/fixture",
    ):
        assert Curl().request("POST", url, payload) == Response(201, payload)
    assert "Authorization: Bearer fixture-secret-token" in commands[0]
    assert all(
        not any("Authorization:" in arg for arg in command) for command in commands[1:]
    )
    assert all(process.returncode == 0 for process in processes)


def test_curl_budget_abort_kills_and_reaps_live_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "RESPONSE_BYTES", 8)
    processes, _commands = controlled_curl(
        monkeypatch, "import os\nwhile True: os.write(1,b'x'*4096)"
    )

    with pytest.raises(Rejected, match="byte budget"):
        Curl().request("GET", "https://delivery.invalid/")

    assert processes[0].returncode is not None and processes[0].returncode < 0
    assert processes[0].stdout is not None and processes[0].stdout.closed


def test_curl_read_error_kills_and_reaps_live_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes, _commands = controlled_curl(
        monkeypatch, "import os\nwhile True: os.write(1,b'x'*4096)"
    )
    original_read = http.os.read

    def fail(fd: int, size: int) -> bytes:
        if processes:
            raise OSError("fixture read failure")
        return original_read(fd, size)

    monkeypatch.setattr(http.os, "read", fail)

    with pytest.raises(Rejected, match="HTTP transport failed"):
        Curl().request("GET", "https://delivery.invalid/")

    assert processes[0].returncode is not None and processes[0].returncode < 0
    assert processes[0].stdout is not None and processes[0].stdout.closed


@pytest.mark.parametrize("suffix", [b"", b"\n20", b"\nwat", b"x200"])
def test_curl_rejects_malformed_status_without_partial_response(
    monkeypatch: pytest.MonkeyPatch, suffix: bytes
) -> None:
    processes, _commands = controlled_curl(
        monkeypatch, f"import sys; sys.stdout.buffer.write({suffix!r})"
    )
    with pytest.raises(Rejected, match="invalid status"):
        Curl().request("GET", "https://delivery.invalid/")
    assert processes[0].returncode == 0


def tarred(files: dict[str, bytes], *, compressed: bool) -> bytes:
    result = io.BytesIO()
    with (
        tarfile.open(fileobj=result, mode="w:gz")
        if compressed
        else tarfile.open(fileobj=result, mode="w:")
    ) as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


def sized_distribution(kind: str) -> tuple[dict[str, bytes], dict[str, bytes]]:
    contents = {"pywhales/py.typed": b"", "payload": b"x" * 128, "second": b"y" * 64}
    metadata = b"Name: pywhales\nVersion: 0.6.1\n"
    files = distributions()
    if kind == "wheel":
        contents["pywhales-0.6.1.dist-info/METADATA"] = metadata
        files[WHEEL] = zipped(contents, compression=zipfile.ZIP_DEFLATED)
    else:
        contents["PKG-INFO"] = metadata
        contents = {f"pywhales-0.6.1/{name}": data for name, data in contents.items()}
        files[SDIST] = tarred(contents, compressed=True)
    return files, contents


@pytest.mark.parametrize("kind", ["artifact", "wheel", "sdist", "pages"])
@pytest.mark.parametrize("axis", ["entries", "member_bytes", "expanded_bytes"])
@pytest.mark.parametrize("excess", [False, True])
def test_all_archive_entries_accept_exact_budgets_and_reject_first_excess(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    axis: str,
    excess: bool,
) -> None:
    contents = {"index.html": b"x" * 128, "second": b"y" * 64}
    files = distributions()
    if kind in {"wheel", "sdist"}:
        files, contents = sized_distribution(kind)
    policy = {
        "entries": len(contents),
        "member_bytes": max(map(len, contents.values())),
        "expanded_bytes": sum(map(len, contents.values())),
    }
    policy[axis] -= int(excess)
    bound = limits.ArchiveLimits(**policy)
    monkeypatch.setattr(
        limits,
        {
            "artifact": "ARTIFACT",
            "wheel": "DISTRIBUTION",
            "sdist": "DISTRIBUTION",
            "pages": "SITE",
        }[kind],
        bound,
    )
    artifact = service.artifact(55, "site", service.source, contents)
    wrapped = zipped(contents, compression=zipfile.ZIP_DEFLATED)
    artifact = artifact.model_copy(update={"digest": f"sha256:{sha256(wrapped)}"})
    service.routes[("GET", BASE_URL + "actions/artifacts/55/zip")] = Response(
        200, wrapped
    )
    site = tmp_path / "site"
    site.mkdir()
    for filename, data in contents.items():
        target = site / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    archive = tmp_path / "artifact.tar"
    archive.write_bytes(tarred(contents, compressed=False))

    def verify() -> None:
        if kind == "artifact":
            assert download(service.api, artifact) == contents
        elif kind in {"wheel", "sdist"}:
            inspect(files, "0.6.1")
        else:
            verify_pages_tar(site, archive)

    if excess:
        with pytest.raises(Rejected, match="budget exceeded"):
            verify()
    else:
        verify()


@pytest.mark.parametrize("kind", ["artifact", "wheel", "sdist", "pages"])
def test_dishonest_member_metadata_cannot_bypass_actual_expansion(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    contents = {"payload": b"x"}
    files = distributions()
    if kind in {"wheel", "sdist"}:
        files, contents = sized_distribution(kind)
        contents = {
            name: b"x" if name.endswith("payload") else data
            for name, data in contents.items()
        }
        files[WHEEL if kind == "wheel" else SDIST] = (
            zipped(contents) if kind == "wheel" else tarred(contents, compressed=True)
        )
    artifact = service.artifact(55, "site", service.source, contents)
    archive_path = tmp_path / "artifact.tar"
    archive_path.write_bytes(tarred(contents, compressed=False))
    site = tmp_path / "site"
    site.mkdir()
    (site / "payload").write_bytes(b"x")
    monkeypatch.setattr(limits, "ARTIFACT", limits.ArchiveLimits(100, 127, 1024))
    monkeypatch.setattr(limits, "DISTRIBUTION", limits.ArchiveLimits(100, 127, 1024))
    monkeypatch.setattr(limits, "SITE", limits.ArchiveLimits(100, 127, 1024))
    zip_open = zipfile.ZipFile.open
    tar_open = tarfile.TarFile.extractfile

    def zip_stream(package: zipfile.ZipFile, entry: zipfile.ZipInfo) -> IO[bytes]:
        if entry.filename.endswith("payload"):
            return io.BytesIO(b"x" * 128)
        return zip_open(package, entry)

    def tar_stream(
        package: tarfile.TarFile, member: tarfile.TarInfo
    ) -> IO[bytes] | None:
        if member.name.endswith("payload"):
            return io.BytesIO(b"x" * 128)
        return tar_open(package, member)

    monkeypatch.setattr(zipfile.ZipFile, "open", zip_stream)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", tar_stream)

    with pytest.raises(Rejected, match="input byte budget exceeded"):
        if kind == "artifact":
            download(service.api, artifact)
        elif kind in {"wheel", "sdist"}:
            inspect(files, "0.6.1")
        else:
            verify_pages_tar(site, archive_path)


@pytest.mark.parametrize("kind", ["project", "wheel", "sdist"])
@pytest.mark.parametrize("excess", [False, True])
def test_local_smoke_bounds_each_input_before_isolated_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, excess: bool
) -> None:
    files = distributions()
    project = ('[project]\nversion = "0.6.1"\n# ' + "é" * 64 + "\n").encode()
    selected = {"project": "pyproject.toml", "wheel": WHEEL, "sdist": SDIST}[kind]
    if kind != "project":
        files[selected] += b"\0" * 8192
    (tmp_path / "pyproject.toml").write_bytes(project)
    (tmp_path / "dist").mkdir()
    for filename, data in files.items():
        (tmp_path / "dist" / filename).write_bytes(data)
    monkeypatch.setattr(
        limits,
        "PROJECT_BYTES" if kind == "project" else "DISTRIBUTION_BYTES",
        len(project if kind == "project" else files[selected]) - int(excess),
    )
    installed: list[list[str]] = []

    def install(
        command: list[str], *, cwd: str, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        installed.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", install)

    if excess:
        with pytest.raises(Rejected, match="input byte budget exceeded"):
            smoke(tmp_path)
        assert installed == []
    else:
        smoke(tmp_path)
        assert len(installed) == 2


@pytest.mark.parametrize("excess", [False, True])
def test_sealer_independently_bounds_project_configuration(
    service: Service,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excess: bool,
) -> None:
    project = ('[project]\nversion = "0.6.1"\n# ' + "é" * 64 + "\n").encode()
    checkout(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(project)
    monkeypatch.setattr(limits, "PROJECT_BYTES", len(project) - int(excess))
    uploaded(service, monkeypatch)

    if excess:
        with pytest.raises(Rejected, match="input byte budget exceeded"):
            seal(service.api, 100, tmp_path)
    else:
        assert seal(service.api, 100, tmp_path) == service.tested


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("excess", [False, True])
def test_distribution_inspection_independently_bounds_compressed_inputs(
    monkeypatch: pytest.MonkeyPatch, kind: str, excess: bool
) -> None:
    files = distributions()
    selected = WHEEL if kind == "wheel" else SDIST
    files[selected] += b"\0" * 8192
    monkeypatch.setattr(
        limits, "DISTRIBUTION_BYTES", len(files[selected]) - int(excess)
    )

    if excess:
        with pytest.raises(Rejected, match="distribution input byte budget"):
            inspect(files, "0.6.1")
    else:
        inspect(files, "0.6.1")


@pytest.mark.parametrize("excess", [False, True])
def test_pages_independently_bounds_actual_local_tar_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excess: bool
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_bytes(b"site")
    data = tarred({"index.html": b"site"}, compressed=False)
    archive = tmp_path / "artifact.tar"
    archive.write_bytes(data)
    monkeypatch.setattr(limits, "PAGES_TAR_BYTES", len(data) - int(excess))

    if excess:
        with pytest.raises(Rejected, match="input byte budget exceeded"):
            verify_pages_tar(site, archive)
    else:
        verify_pages_tar(site, archive)


@pytest.mark.parametrize("axis", ["entries", "member_bytes", "expanded_bytes"])
def test_pages_reference_site_has_its_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, axis: str
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_bytes(b"x" * 8)
    (site / "second").write_bytes(b"y" * 8)
    data = tarred({"index.html": b"", "second": b""}, compressed=False)
    archive = tmp_path / "artifact.tar"
    archive.write_bytes(data)
    policy = {"entries": 2, "member_bytes": 8, "expanded_bytes": 16}
    policy[axis] -= 1
    monkeypatch.setattr(limits, "SITE", limits.ArchiveLimits(**policy))

    if axis == "entries":
        archive.write_bytes(tarred({"index.html": b""}, compressed=False))
    with pytest.raises(Rejected, match="budget exceeded"):
        verify_pages_tar(site, archive)


def test_bounded_read_stops_on_first_excess_without_unbounded_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Source(io.BytesIO):
        @override
        def read(self, size: int | None = -1, /) -> bytes:
            assert size is not None and 0 < size <= 5 - self.tell()
            return super().read(min(size, 2))

    source = Source(b"abcdefgh" * 1000)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: source)

    with pytest.raises(Rejected, match="input byte budget exceeded"):
        budgets.local(tmp_path / "growing-file", 4)
    assert source.closed


def test_digest_rejection_precedes_any_zip_member_trust(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    def decode(data: bytes, policy: limits.ArchiveLimits) -> dict[str, bytes]:
        pytest.fail("member parser used before digest verification")

    artifact = service.artifacts()[0].model_copy(
        update={"digest": f"sha256:{'0' * 64}"}
    )
    monkeypatch.setattr(artifacts, "zip_files", decode)

    with pytest.raises(Rejected, match="archive digest mismatch"):
        download(service.api, artifact)


def test_rejected_extraction_does_not_publish_partial_files(tmp_path: Path) -> None:
    destination = tmp_path / "output"

    with pytest.raises(OSError):
        artifacts.extract(
            {"first": b"accepted", "first/nested": b"conflict"}, destination
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["artifact", "wheel", "sdist", "pages"])
@pytest.mark.parametrize("fault", ["malformed", "path", "duplicate", "special"])
def test_all_archive_entry_points_reject_unsafe_or_ambiguous_members(
    service: Service, tmp_path: Path, kind: str, fault: str
) -> None:
    result = io.BytesIO()
    name = "../escape" if fault == "path" else "payload"
    if kind in {"artifact", "wheel"}:
        with zipfile.ZipFile(result, "w") as package:
            entry = zipfile.ZipInfo(name)
            if fault == "special":
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
            package.writestr(entry, b"data")
            if fault == "duplicate":
                with pytest.warns(UserWarning):
                    package.writestr(name, b"second")
    else:
        with (
            tarfile.open(fileobj=result, mode="w:gz")
            if kind == "sdist"
            else tarfile.open(fileobj=result, mode="w:")
        ) as tar:
            entry = tarfile.TarInfo(name)
            if fault == "special":
                entry.type = tarfile.SYMTYPE
                entry.linkname = "target"
            tar.addfile(entry)
            if fault == "duplicate":
                tar.addfile(tarfile.TarInfo(name))
    data = b"broken" if fault == "malformed" else result.getvalue()
    artifact = service.artifact(55, "site", service.source, {"placeholder": b""})
    artifact = artifact.model_copy(update={"digest": f"sha256:{sha256(data)}"})
    service.routes[("GET", BASE_URL + "actions/artifacts/55/zip")] = Response(200, data)
    files = distributions() | {WHEEL if kind == "wheel" else SDIST: data}
    site = tmp_path / "site"
    site.mkdir()
    (site / "payload").write_bytes(b"")
    archive = tmp_path / "artifact.tar"
    archive.write_bytes(data)
    output = tmp_path / "output"

    reason = {
        "malformed": "invalid .* archive",
        "path": "unsafe archive path",
        "duplicate": "duplicate archive path",
        "special": "archive special file",
    }[fault]
    with pytest.raises(Rejected, match=reason):
        if kind == "artifact":
            artifacts.extract(download(service.api, artifact), output)
        elif kind in {"wheel", "sdist"}:
            inspect(files, "0.6.1")
        else:
            verify_pages_tar(site, archive)
    assert not output.exists()


@pytest.mark.parametrize("tar", [False, True])
@pytest.mark.parametrize("excess", [False, True])
def test_directory_entries_count_even_without_payload(tar: bool, excess: bool) -> None:
    result = io.BytesIO()
    if tar:
        with tarfile.open(fileobj=result, mode="w:") as package:
            for name in ("first", "second"):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                package.addfile(entry)
    else:
        with zipfile.ZipFile(result, "w") as package:
            for name in ("first/", "second/"):
                package.writestr(name, b"")
    policy = limits.ArchiveLimits(2 - int(excess), 0, 0)

    def decode() -> dict[str, bytes]:
        if tar:
            return archives.tar_files(result.getvalue(), policy, pages=True)
        return archives.zip_files(result.getvalue(), policy)

    if excess:
        with pytest.raises(Rejected, match="entry budget exceeded"):
            decode()
    else:
        assert decode() == {}


def test_artifact_response_budget_is_independent_of_transport(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = service.artifact(55, "site", service.source, {"payload": b""})
    data = service.routes[("GET", BASE_URL + "actions/artifacts/55/zip")].body
    monkeypatch.setattr(limits, "RESPONSE_BYTES", len(data))
    assert download(service.api, artifact) == {"payload": b""}
    monkeypatch.setattr(limits, "RESPONSE_BYTES", len(data) - 1)

    with pytest.raises(Rejected, match="artifact input byte budget"):
        download(service.api, artifact)


def test_zip_nul_name_cannot_be_truncated_into_a_trusted_path() -> None:
    data = zipped({"aabb": b"data"}).replace(b"aabb", b"aa\0b")

    with pytest.raises(Rejected, match="unsafe archive path"):
        archives.zip_files(data, limits.ARTIFACT)


@pytest.mark.parametrize("tar", [False, True])
def test_high_ratio_compressed_members_exceed_small_expansion_budget(tar: bool) -> None:
    contents = {"payload": b"x" * (2 * 1024 * 1024)}
    data = (
        tarred(contents, compressed=True)
        if tar
        else zipped(contents, compression=zipfile.ZIP_DEFLATED)
    )
    assert len(data) < 4096
    policy = limits.ArchiveLimits(10, 65536, 65536)

    with pytest.raises(Rejected, match="byte budget exceeded"):
        if tar:
            archives.tar_files(data, policy)
        else:
            archives.zip_files(data, policy)


def test_pages_rejects_reference_symlink(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_bytes(b"data")
    archive = tmp_path / "artifact.tar"
    archive.write_bytes(tarred({"index.html": b"data"}, compressed=False))

    with pytest.raises(Rejected, match="Pages site special file"):
        verify_pages_tar(site, archive)


def dumped(model: BaseModel) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_json(model.model_dump_json())


def job_item(number: int) -> dict[str, JsonValue]:
    return dumped(
        Job(
            id=number,
            name=f"job-{number}",
            run_id=100,
            run_attempt=1,
            head_sha="a" * 40,
            status="completed",
            conclusion="success",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )


def artifact_item(number: int) -> dict[str, JsonValue]:
    return dumped(
        Artifact(
            id=number,
            name=f"artifact-{number}",
            expired=False,
            digest=f"sha256:{'0' * 64}",
            created_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            workflow_run=ArtifactRun(
                id=100,
                repository_id=1,
                head_repository_id=1,
                head_sha="a" * 40,
                head_branch="main",
            ),
        )
    )


@dataclass(frozen=True)
class Listing:
    """One paginated endpoint: its cap, path, envelope and item identity."""

    limit: str
    path: str
    model: type[BaseModel]
    envelope: str | None
    item: Callable[[int], dict[str, JsonValue]]

    def fetch(self, api: GitHub) -> tuple[BaseModel, ...]:
        return api.pages(self.path, self.model, self.envelope)

    def items(self, start: int, count: int) -> list[JsonValue]:
        return [self.item(number) for number in range(start, start + count)]

    def page(self, items: list[JsonValue], total: int | None = None) -> Response:
        if self.envelope is None:
            return Response(200, json.dumps(items).encode())
        count = len(items) if total is None else total
        body = {"total_count": count, self.envelope: items}
        return Response(200, json.dumps(body).encode())

    def url(self, page: int) -> str:
        return f"{self.path}?per_page=100&page={page}"


LISTINGS = {
    "jobs": Listing(
        "JOB_ITEMS", "actions/runs/100/attempts/1/jobs", Job, "jobs", job_item
    ),
    "artifacts": Listing(
        "ARTIFACT_ITEMS",
        "actions/runs/100/artifacts",
        Artifact,
        "artifacts",
        artifact_item,
    ),
    "releases": Listing(
        "RELEASE_ITEMS",
        "releases",
        Release,
        None,
        lambda number: {
            "id": number,
            "tag_name": f"v{number}",
            "draft": False,
            "prerelease": False,
        },
    ),
    "pulls": Listing(
        "PULL_ITEMS",
        f"commits/{'a' * 40}/pulls",
        PullNumber,
        None,
        lambda number: {"number": number},
    ),
}
ENVELOPED = [name for name, listing in LISTINGS.items() if listing.envelope is not None]


@dataclass
class Pager(Transport):
    """Serve each numbered page from a callback and record the requested URLs."""

    respond: Callable[[int], Response]
    urls: list[str] = field(default_factory=list)

    @override
    def request(self, method: str, url: str, body: bytes | None = None) -> Response:
        if "?" not in url:
            return Response(404, b"{}")
        self.urls.append(url.removeprefix(BASE_URL))
        return self.respond(int(url.rsplit("page=", 1)[1]))

    def api(self) -> GitHub:
        return GitHub(self, "owner/repo", 1)


def sequence(listing: Listing, *sizes: int) -> Pager:
    """Pages of consecutive distinct items with sizes; later pages are empty."""
    starts = [sum(sizes[:index]) + 1 for index in range(len(sizes))]
    total = sum(sizes)
    return Pager(
        lambda page: listing.page(
            listing.items(starts[page - 1], sizes[page - 1])
            if page <= len(sizes)
            else [],
            total,
        )
    )


@pytest.fixture(params=LISTINGS)
def listing(request: pytest.FixtureRequest) -> Listing:
    return LISTINGS[request.param]


def test_each_endpoint_cap_passes_at_limit_and_fails_one_past(
    listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, listing.limit, 3)
    exact = sequence(listing, 3)
    assert len(listing.fetch(exact.api())) == 3
    assert exact.urls == [listing.url(1)]

    over = Pager(lambda _page: listing.page(listing.items(1, 4)))
    with pytest.raises(Rejected, match="item budget exceeded"):
        listing.fetch(over.api())
    assert over.urls == [listing.url(1)]


@pytest.mark.parametrize("pages", [1, 2])
def test_full_final_page_needs_an_empty_terminator_request(
    listing: Listing, monkeypatch: pytest.MonkeyPatch, pages: int
) -> None:
    monkeypatch.setattr(limits, listing.limit, 100 * pages)
    server = sequence(listing, *[100] * pages)
    assert len(listing.fetch(server.api())) == 100 * pages
    assert server.urls == [listing.url(page) for page in range(1, pages + 2)]


def test_item_past_a_full_page_cap_fails_on_the_terminator(
    listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, listing.limit, 100)
    server = sequence(listing, 100, 1)
    with pytest.raises(Rejected, match="item budget exceeded"):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2)]


@pytest.mark.parametrize("fresh", [False, True])
def test_server_ignoring_page_is_stopped_within_the_request_budget(
    listing: Listing, monkeypatch: pytest.MonkeyPatch, fresh: bool
) -> None:
    monkeypatch.setattr(limits, listing.limit, 100 if fresh else 200)
    server = Pager(
        lambda page: listing.page(listing.items(page if fresh else 1, 100), 100)
    )
    reason = "item budget exceeded" if fresh else "repeated item"
    with pytest.raises(Rejected, match=reason):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2)]


@pytest.mark.parametrize("within_page", [False, True])
def test_repeated_item_identity_is_rejected(
    listing: Listing, monkeypatch: pytest.MonkeyPatch, within_page: bool
) -> None:
    monkeypatch.setattr(limits, listing.limit, 200)
    first = listing.items(1, 100)
    second = first[-1:] + listing.items(101, 3)
    pages = [first + first[:1], second] if within_page else [first, second]
    server = Pager(lambda page: listing.page(pages[page - 1], 103))
    with pytest.raises(Rejected, match="repeated item"):
        listing.fetch(server.api())


def test_pull_identity_is_the_number_not_an_id() -> None:
    listing = LISTINGS["pulls"]
    server = Pager(lambda _page: listing.page([{"number": 7}, {"number": 7}]))
    with pytest.raises(Rejected, match="repeated item"):
        listing.fetch(server.api())


def test_cumulative_bytes_pass_at_limit_and_fail_one_past(
    listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, listing.limit, 150)
    sizes = [len(page.body) for page in sequence_pages(listing, 100, 50)]
    monkeypatch.setattr(limits, "PAGINATION_BYTES", sum(sizes))
    exact = sequence(listing, 100, 50)
    assert len(listing.fetch(exact.api())) == 150

    monkeypatch.setattr(limits, "PAGINATION_BYTES", sum(sizes) - 1)
    over = sequence(listing, 100, 50)
    with pytest.raises(Rejected, match="byte budget exceeded"):
        listing.fetch(over.api())
    assert over.urls == [listing.url(1), listing.url(2)]


def sequence_pages(listing: Listing, *sizes: int) -> list[Response]:
    server = sequence(listing, *sizes)
    return [server.respond(page) for page in range(1, len(sizes) + 1)]


def test_first_page_alone_is_held_to_the_cumulative_byte_budget(
    listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = sequence_pages(listing, 2)[0].body
    monkeypatch.setattr(limits, "PAGINATION_BYTES", len(body) - 1)
    with pytest.raises(Rejected, match="byte budget exceeded"):
        listing.fetch(sequence(listing, 2).api())


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non_success_page_fails_instead_of_ending_pagination(
    listing: Listing, status: int
) -> None:
    server = Pager(
        lambda page: (
            listing.page(listing.items(1, 100), 150)
            if page == 1
            else Response(status, b"{}")
        )
    )
    with pytest.raises(Rejected, match="pagination response"):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2)]


def test_rejections_carry_no_response_payload(listing: Listing) -> None:
    secret = "SECRET-TOKEN"
    malformed = [
        Response(200, f"not json {secret}".encode()),
        listing.page([{"bogus": secret}]),
        Response(200, json.dumps({"total_count": secret}).encode()),
        Response(200, json.dumps({"total_count": 1, "other": [secret]}).encode()),
    ]
    for response in malformed:
        with pytest.raises(Rejected) as caught:
            listing.fetch(Pager(lambda _page, response=response: response).api())
        assert secret not in caught.value.reason


@pytest.mark.parametrize("name", ENVELOPED)
@pytest.mark.parametrize("total", ["absent", "text", "boolean", "negative", "null"])
def test_envelope_without_a_valid_total_count_is_rejected(
    name: str, total: str
) -> None:
    listing = LISTINGS[name]
    assert listing.envelope is not None
    body: dict[str, JsonValue] = {listing.envelope: listing.items(1, 2)}
    values: dict[str, JsonValue] = {
        "text": "2",
        "boolean": True,
        "negative": -1,
        "null": None,
    }
    if total != "absent":
        body["total_count"] = values[total]
    server = Pager(lambda _page: Response(200, json.dumps(body).encode()))
    with pytest.raises(Rejected, match="total_count missing"):
        listing.fetch(server.api())


@pytest.mark.parametrize("name", ENVELOPED)
def test_total_count_must_not_drift_between_pages(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = LISTINGS[name]
    monkeypatch.setattr(limits, listing.limit, 200)
    server = Pager(
        lambda page: listing.page(
            listing.items(100 * (page - 1) + 1, 100 if page == 1 else 5),
            150 if page == 1 else 105,
        )
    )
    with pytest.raises(Rejected, match="total_count changed"):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2)]


@pytest.mark.parametrize("name", ENVELOPED)
@pytest.mark.parametrize("total", [4, 6])
def test_final_count_must_equal_total_count(name: str, total: int) -> None:
    listing = LISTINGS[name]
    server = Pager(lambda _page: listing.page(listing.items(1, 5), total))
    with pytest.raises(Rejected, match="total_count mismatch"):
        listing.fetch(server.api())


@pytest.mark.parametrize("name", ENVELOPED)
def test_server_truncation_to_a_full_page_is_detected(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = LISTINGS[name]
    monkeypatch.setattr(limits, listing.limit, 200)
    server = Pager(
        lambda page: listing.page(listing.items(1, 100) if page == 1 else [], 150)
    )
    with pytest.raises(Rejected, match="total_count mismatch"):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2)]


@pytest.mark.parametrize("name", ENVELOPED)
def test_total_count_never_ends_pagination_early(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = LISTINGS[name]
    monkeypatch.setattr(limits, listing.limit, 200)
    server = Pager(
        lambda page: listing.page(listing.items(100 * (page - 1) + 1, 100), 100)
    )
    with pytest.raises(Rejected, match="item budget exceeded"):
        listing.fetch(server.api())
    assert server.urls == [listing.url(1), listing.url(2), listing.url(3)]


def test_model_without_a_policy_cannot_paginate() -> None:
    server = Pager(lambda _page: Response(200, b"[]"))
    with pytest.raises(Rejected, match="no item cap"):
        server.api().pages("things", Repository)
    assert server.urls == []


def test_callers_use_the_bounded_paginator(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = service.source
    monkeypatch.setattr(limits, "JOB_ITEMS", 2)
    monkeypatch.setattr(limits, "ARTIFACT_ITEMS", 2)
    monkeypatch.setattr(limits, "RELEASE_ITEMS", 2)
    monkeypatch.setattr(limits, "PULL_ITEMS", 2)
    service.set_jobs(service.jobs[:2])
    service.set_artifacts(service.artifacts()[:2])
    service.put(
        f"commits/{run.head_sha}/pulls?per_page=100&page=1",
        [{"number": 7}, {"number": 8}],
    )
    service.put(
        "releases?per_page=100&page=1",
        [LISTINGS["releases"].item(number) for number in range(1, 3)],
    )
    assert len(attempt_jobs(service.api, run)) == 2
    assert len(inventory(service.api, run)) == 2
    assert release_record(service.api, "v9") is None
    merged_pull(service.api, run)

    service.set_jobs(service.jobs[:3])
    service.set_artifacts((*service.artifacts(), service.artifacts()[0]))
    service.put(
        f"commits/{run.head_sha}/pulls?per_page=100&page=1",
        [{"number": 7}, {"number": 8}, {"number": 9}],
    )
    service.put(
        "releases?per_page=100&page=1",
        [LISTINGS["releases"].item(number) for number in range(1, 4)],
    )
    with pytest.raises(Rejected, match="item budget exceeded"):
        attempt_jobs(service.api, run)
    with pytest.raises(Rejected, match=r"repeated item|item budget exceeded"):
        inventory(service.api, run)
    with pytest.raises(Rejected, match="item budget exceeded"):
        release_record(service.api, "v9")
    with pytest.raises(Rejected, match="item budget exceeded"):
        merged_pull(service.api, run)


@pytest.mark.parametrize("fault", ["missing", "mismatch"])
def test_job_and_artifact_callers_reject_unreliable_total_count(
    service: Service, fault: str
) -> None:
    run = service.source
    jobs: list[JsonValue] = [dumped(job) for job in service.jobs]
    found: list[JsonValue] = [dumped(item) for item in service.artifacts()]
    for path, items, key, call in (
        ("actions/runs/100/attempts/1/jobs", jobs, "jobs", attempt_jobs),
        ("actions/runs/100/artifacts", found, "artifacts", inventory),
    ):
        envelope: dict[str, JsonValue] = {key: items}
        if fault == "mismatch":
            envelope["total_count"] = len(items) + 1
        service.put(f"{path}?per_page=100&page=1", envelope)
        reason = "total_count missing" if fault == "missing" else "total_count mismatch"
        with pytest.raises(Rejected, match=reason):
            call(service.api, run)


JSON_BODY_EDGES: dict[str, tuple[JsonValue, bytes]] = {
    "unicode": (
        {"text": "caf\u00e9 \u2014 \u65e5\u672c \U0001f600"},
        b'{"text": "caf\\u00e9 \\u2014 \\u65e5\\u672c \\ud83d\\ude00"}',
    ),
    "unpaired-surrogates": ({"text": "\ud800x\udc00"}, b'{"text": "\\ud800x\\udc00"}'),
    "control-characters": (
        '\x00\x1f\x7f/"\\',
        b'"\\u0000\\u001f\\u007f/\\"\\\\"',
    ),
    "non-finite": (
        [float("nan"), float("inf"), float("-inf")],
        b"[NaN, Infinity, -Infinity]",
    ),
    "oversized-integers": (
        {"n": 2**70, "m": -(2**64)},
        b'{"n": 1180591620717411303424, "m": -18446744073709551616}',
    ),
}
JSON_NOTES = "### Added\n\n- Caf\u00e9 \u2014 \u65e5\u672c\u8a9e \U0001f600 a\u2028b"
JSON_NOTES_WIRE = b'"### Added\\n\\n- Caf\\u00e9 \\u2014 \\u65e5\\u672c\\u8a9e \\ud83d\\ude00 a\\u2028b"'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({}, b"{}"),
        ([], b"[]"),
        (None, b"null"),
        ("plain", b'"plain"'),
        (
            {"ref": "refs/tags/v1", "sha": "a" * 40},
            b'{"ref": "refs/tags/v1", "sha": "' + b"a" * 40 + b'"}',
        ),
        (
            {"body": "x", "draft": False, "n": [1, 2.5, None, True]},
            b'{"body": "x", "draft": false, "n": [1, 2.5, null, true]}',
        ),
    ],
)
def test_json_body_uses_stdlib_default_separators(
    value: JsonValue, expected: bytes
) -> None:
    body = http.json_body(value)
    assert type(body) is bytes
    assert body == expected


@pytest.mark.parametrize("case", JSON_BODY_EDGES)
def test_json_body_serializes_arbitrary_json_values_exactly(case: str) -> None:
    value, expected = JSON_BODY_EDGES[case]
    body = http.json_body(value)
    assert body == expected and body.isascii()
    assert json.dumps(json.loads(body)).encode() == expected


def test_parse_item_validates_in_json_mode_for_strict_models() -> None:
    item = job_item(7)
    with pytest.raises(ValidationError):
        Job.model_validate(item)

    parsed = http.parse_item(Job, item)

    assert parsed.id == 7
    assert parsed.started_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert parsed.completed_at == datetime(2026, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("body", float("nan")),
        ("body", float("inf")),
        ("body", float("-inf")),
        ("id", float("nan")),
        ("tag_name", "\ud800"),
    ],
    ids=["nan-body", "inf-body", "negative-inf-body", "nan-id", "unpaired-surrogate"],
)
def test_parse_item_rejects_non_finite_and_unpaired_surrogate_values(
    field_name: str, value: JsonValue
) -> None:
    item = {**LISTINGS["releases"].item(1), field_name: value}

    with pytest.raises(Rejected) as rejected:
        http.parse_item(Release, item)

    assert str(rejected.value) == "pagination item malformed"


def test_parse_item_keeps_integers_beyond_64_bits_and_unicode_exact() -> None:
    oversized = 2**70
    tag = "v\u00e9\u65e5\U0001f600"
    item = {
        **LISTINGS["releases"].item(1),
        "id": oversized,
        "tag_name": tag,
        "body": "a\u2028b",
    }

    release = http.parse_item(Release, item)

    assert type(release.id) is int and release.id == oversized
    assert release.tag_name == tag and release.body == "a\u2028b"
    assert http.parse_item(PullNumber, {"number": oversized}).number == oversized


def test_pages_keep_distinct_integers_beyond_double_precision() -> None:
    first, second = 2**70, 2**70 + 1
    assert float(first) == float(second)
    listing = LISTINGS["releases"]
    distinct: list[JsonValue] = [
        {**listing.item(1), "id": first},
        {**listing.item(2), "id": second},
    ]
    server = Pager(lambda _page: listing.page(distinct))

    found = server.api().pages(listing.path, Release)

    assert [release.id for release in found] == [first, second]
    assert server.urls == [listing.url(1)]

    repeated: list[JsonValue] = [
        {**listing.item(1), "id": first},
        {**listing.item(2), "id": first},
    ]
    server = Pager(lambda _page: listing.page(repeated))
    with pytest.raises(Rejected, match="repeated item"):
        server.api().pages(listing.path, Release)


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_pages_reject_non_finite_tokens_inside_items(token: bytes) -> None:
    body = (
        b'[{"id": 1, "tag_name": "v1", "draft": false, "prerelease": false, '
        b'"body": ' + token + b"}]"
    )
    server = Pager(lambda _page: Response(200, body))

    with pytest.raises(Rejected, match="pagination item malformed"):
        server.api().pages("releases", Release)

    assert server.urls == [LISTINGS["releases"].url(1)]


@dataclass
class Recorder(Transport):
    """Wrap a transport and keep every request body exactly as sent."""

    inner: Transport
    writes: list[tuple[str, str, bytes]] = field(default_factory=list)

    @override
    def request(self, method: str, url: str, body: bytes | None = None) -> Response:
        if body is not None:
            self.writes.append((method, url.removeprefix(BASE_URL), body))
        return self.inner.request(method, url, body)


def test_release_requests_send_unicode_notes_as_ascii_json_bytes(
    service: Service,
) -> None:
    sealed = sealed_release(service)
    document = sealed.model_copy(
        update={"seal": sealed.seal.model_copy(update={"notes": JSON_NOTES})}
    )
    fake = ReleaseAPI(service)
    recorder = Recorder(fake)

    release(GitHub(recorder, "owner/repo", 1), document)

    assert [(method, path) for method, path, _body in recorder.writes] == [
        ("POST", "releases"),
        ("PATCH", "releases/1"),
    ]
    created, published = (body for _method, _path, body in recorder.writes)
    assert type(created) is bytes and created.isascii()
    assert type(published) is bytes and published.isascii()
    assert created == (
        b'{"tag_name": "v0.6.1", "name": "v0.6.1", "target_commitish": "'
        + b"a" * 40
        + b'", "body": '
        + JSON_NOTES_WIRE
        + b', "draft": true, "prerelease": false}'
    )
    assert published == b'{"body": ' + JSON_NOTES_WIRE + b', "draft": false}'
    assert fake.current == Release(
        id=1, tag_name="v0.6.1", draft=False, prerelease=False, body=JSON_NOTES
    )
