# Delivery Chain Guide

CI-only trust chain, not an installed API. `README.md` here is authoritative for the handoffs, size budgets, and
pagination rationale; this guide is the editing map. Local tests drive the real decisions through a stubbed `Transport`.

## Local Map

| File | Role |
| --- | --- |
| `__main__.py` | CLI: `smoke seal prepare confirm release pages verify-pages`; failure exits 1 as `delivery rejected: <reason>` |
| `api.py` | `Curl` (HTTPS only; 10 s connect, 120 s curl, 130 s supervisor), `GitHub.pages`, `Rejected`, `require` |
| `models.py`, `limits.py` | Typed GitHub, PyPI, `Seal` and `Receipt` documents; every byte, entry, and item cap |
| `budgets.py`, `archives.py` | Incremental payload accounting; data-only ZIP and tar decoders with `safe_path` |
| `artifacts.py`, `chain.py` | Inventory, selection, download, verification; `seal`, `producer`, `receipt`, artifact names |
| `trust.py` | Job-name constants, `trusted_run`, `attempt_jobs`, `successful_jobs`, `merged_pull` |
| `distributions.py`, `notes.py` | Distribution `inspect` and `smoke`; exact `## <version>` section and body comparison |
| `publication.py`, `releases.py`, `pages.py` | PyPI `destination`, `prepare`, `confirm`; tag and release; `verify_pages_tar` |

Workflows: `python-package.yml` runs `smoke`, then `seal` on push to `main` only. `python-publish.yml` runs `prepare`,
the uploader, then `confirm`. `release-on-merge.yml` runs `release`, then calls `gh-pages.yml`: `pages`, `verify-pages`.

## Trust Rules To Preserve

- `Seal` and `Receipt` are schema version 2. Version 1 is rejected and there's no dual-accept path.
- Authority comes from the invoking repository and the workflow's numeric ID (`GitHub.identity`, `trusted_run`), never
  artifact metadata: same repository, branch `main`, event `push` (producer) or `workflow_run` (publisher), completed
  with success, and the latest attempt endpoint equal to the run.
- Job names are exact. `trust.CHECKS` (eight fixed names plus the 16 `COMPONENTS`) with `SEAL_JOB`, and `PUBLISH_JOB`,
  must equal the attempt's jobs as sets. Renaming a job or editing the `python-package.yml` component matrix needs the
  same change in `trust.py`, and a look at the job cap in `limits.py`.
- Artifacts are `delivery-{dist,site,seal,receipt}-<run>-<attempt>`: exact name (missing, duplicate, or expired rejects),
  linked to run, repository, SHA, and the producing job's interval, fetched by ID, ZIP digest checked before any member is
  read. The seal must match the uploader's `*_ARTIFACT_ID` and `*_ARTIFACT_DIGEST` outputs.
- `release` and `pages` rebuild the producer chain through `receipt()`, then need a fresh `destination()` with nothing
  missing. `prepare` and `confirm` check `PRODUCER_ATTEMPT`; `release` checks `PUBLISHER_ATTEMPT`. Nothing borrows from
  an older attempt, so rerun the whole workflow, not failed jobs. `confirm` retries only missing files (12 times, 5 s
  apart); conflicts and malformed responses fail at once.

## Limits And Pagination

- `GitHub.pages` is the only list paginator (callers: `inventory`, `attempt_jobs`, `release_record`, `merged_pull`). A new
  list endpoint needs a `listed()` entry and a cap in `limits.py`.
- Item caps, 100 per page: jobs 200, artifacts 100, releases 500, commit pulls 100. A listing may make `cap // 100 + 1`
  requests, leaving room for the empty terminator after a full final page; only a short page ends it.
- A listing may read 16 MiB across all pages, independent of the 64 MiB per-response budget. Limits are inclusive: exact
  passes, one over raises `Rejected`. A non-200 page or a repeated `id` (`number` for commit pulls) fails.
- Job and artifact envelopes need a non-negative integer `total_count` on every page, unchanged and equal to the final
  count. Bare-array releases and commit pulls have none, so silent truncation there stays undetectable.

## Workflow And CLI Rules

- `python-publish.yml`, `release-on-merge.yml`, and `gh-pages.yml` start from `permissions: {}`; `python-package.yml`
  starts from `contents: read` and only the seal job adds `actions: read`. Third-party actions pin full commit SHAs.
- Those three consumers check out `github.workflow_sha` with `persist-credentials: false`, handle only data, and never
  install or build downloaded distributions. Publisher and Pages don't cancel in-progress runs; Pages deploys from `main`.
- `smoke` and `verify-pages` are local checks. After `uv build --no-sources --sdist --wheel`, `smoke` runs two
  `uv run --isolated` installs (wheel, sdist) outside the checkout, 300 s each. `verify-pages` compares local files.
- `seal`, `prepare`, `confirm`, `release`, and `pages` need `GH_TOKEN`, `GITHUB_REPOSITORY`, and `GITHUB_REPOSITORY_ID`,
  call GitHub and PyPI, and `release` creates tags and releases. Never run them locally against production.

## Scoped Checks

These come from the README; root ty excludes `scripts/`, so ty needs the override.

```bash
uv run --locked pytest tests/pywhales/test_delivery_workflows.py
uv run --locked ruff check scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked ruff format --check scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked ty check --config 'src.include = ["scripts/delivery", "tests/pywhales/test_delivery_workflows.py"]' scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked python -m scripts.delivery --help
```
