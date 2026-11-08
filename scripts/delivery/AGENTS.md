# Delivery Chain Guide

CI-only trust chain, not an installed API. `README.md` here remains authoritative for helper trust contracts, size budgets,
and pagination rationale. Its Pages handoff description is stale, not proof of an active workflow; current wiring is below.
Local tests drive the real decisions through a stubbed `Transport`.

## Local Map

| File | Role |
| --- | --- |
| `__main__.py` | CLI: `smoke notes seal prepare confirm release pages verify-pages`; failure exits 1 as `delivery rejected: <reason>` |
| `api.py` | `Curl` (HTTPS only; 10 s connect, 120 s curl, 130 s supervisor; bounded `GET` retries), `GitHub.pages`; re-exports `Rejected`, `require` |
| `failures.py`, `deadline.py` | `Rejected`, `CurlFailed` (exit code), `require`; context-local monotonic command deadline (`command_deadline`, `remaining`, `budget`, `afford`) entered once by `__main__.main` |
| `models.py`, `limits.py` | Typed GitHub, PyPI, `Seal` and `Receipt` documents; every byte, entry, and item cap |
| `budgets.py`, `archives.py` | Incremental payload accounting; data-only ZIP and tar decoders with `safe_path` |
| `artifacts.py`, `chain.py` | Inventory, selection, download, verification; `seal`, `producer`, `receipt`, artifact names |
| `trust.py` | Job-name constants, `trusted_run`, `attempt_jobs`, `successful_jobs`, `merged_pull` |
| `distributions.py`, `notes.py` | Distribution `inspect` and `smoke`; exact `## <version>` section (top-level fences hidden) and body comparison |
| `publication.py`, `releases.py`, `pages.py` | PyPI `destination`, `prepare`, `confirm`; tag and release; `verify_pages_tar` |

Only three workflows exist: `python-package.yml` runs `smoke` and `notes` on every PR and push and seals only pushes to `main`; `python-publish.yml` runs
`prepare`, the uploader, then `confirm`; `release-on-merge.yml` ends at `release`. There is no `gh-pages.yml` or current
Pages deployment workflow. `pages` and `verify-pages` remain CLI helpers.

## Trust Rules To Preserve

- `Seal` and `Receipt` are schema version 2. Version 1 is rejected and there's no dual-accept path.
- Authority comes from the invoking repository and the workflow's numeric ID (`GitHub.identity`, `trusted_run`), never
  artifact metadata: same repository, branch `main`, event `push` (producer) or `workflow_run` (publisher), completed
  with success, and the latest attempt endpoint equal to the run.
- Job names are exact. `trust.CHECKS` (nine fixed names plus the 16 `COMPONENTS`) with `SEAL_JOB`, and `PUBLISH_JOB`,
  must equal the attempt's jobs as sets. Renaming a job or editing the `python-package.yml` component matrix needs the
  same change in `trust.py`, and a look at the job cap in `limits.py`.
- Artifacts are `delivery-{dist,site,seal,receipt}-<run>-<attempt>`: exact name (missing, duplicate, or expired rejects),
  linked to run, repository, SHA, and the producing job's interval, fetched by ID, ZIP digest checked before any member is
  read. The seal must match the uploader's `*_ARTIFACT_ID` and `*_ARTIFACT_DIGEST` outputs.
- `release` and `pages` rebuild the producer chain through `receipt()`, then need a fresh `destination()` with nothing
  missing. `prepare` and `confirm` check `PRODUCER_ATTEMPT`; `release` checks `PUBLISHER_ATTEMPT`. Nothing borrows from
  an older attempt, so rerun the whole workflow, not failed jobs. `confirm` retries only missing files (12 times, 5 s
  apart, within the command deadline); conflicts and malformed responses fail at once.

## Transport Retries And Deadline

- Only `Curl.request` retries, and only `GET`: up to `READ_RETRIES` (3) retries after the first attempt on `TRANSIENT_STATUSES` (429, 502, 503, 504) or a
  `CurlFailed` exit in `TRANSIENT_CURL_EXITS` (6, 7, 28, 52, 55, 56), with deterministic doubling backoff capped at
  `READ_BACKOFF_MAX_SECONDS` (8 s); current delays are 1, 2 and 4 s. All constants live in `limits.py`. Callers get one final `Response` or the final failure, so
  pagination counts accepted pages only. Writes are never retried; 403/404 and other nontransient statuses return on the first attempt for caller classification.
  Protocol/TLS exits, byte-budget and invalid-status faults fail without retry. Stderr stays discarded; classification uses the exit code alone.
- `__main__.main` wraps `execute` in one `command_deadline(COMMAND_SECONDS)` (900 s). `Curl.receive` shortens each attempt with
  `budget`; backoff and `confirm` polling call `afford` first. Expired preflight or a wait that cannot finish before expiry raises
  `Rejected("delivery deadline exceeded")` without another curl or sleep. An active attempt's supervisor timeout remains `Rejected("HTTP transport timed out")`, which isn't retried. Tests keep zero-argument `Curl` factories, so the deadline is context-local state.

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

- `python-publish.yml` and `release-on-merge.yml` start from `permissions: {}`; `python-package.yml`
  starts from `contents: read` and only the seal job adds `actions: read`. Third-party actions pin full commit SHAs.
- Both consumers check out trusted `github.workflow_sha` with `persist-credentials: false`, handle only data, and never
  install or build downloaded distributions. The publisher serializes publication without cancelling in-progress runs.
- `notes` reads the pyproject version and validates its `CHANGELOG.md` section without GitHub environment, through the same
  `project_version` and `release_notes` that `seal` uses. The `Validate release notes` job runs it on pull requests too.
- `smoke`, `notes` and `verify-pages` are local checks. After `uv build --no-sources --sdist --wheel`, `smoke` runs two
  `uv run --isolated` installs (wheel, sdist) outside the checkout, 300 s each.
- `pages` validates the receipt, destination and release, then extracts the verified tested site without rebuilding it.
  `verify-pages` compares a local tar against every site file under `limits.py` budgets. Neither helper deploys.
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
