# Tested Delivery Chain

These helpers are contributor CI tools, not installed PyWhales APIs. They use
the existing locked Python/Pydantic environment and runner `curl`. Local tests
replace every external service with inert fixtures. Do not run publication,
release or deployment operations locally against the production repository.

## Handoffs

1. `python-package.yml` keeps every original required job. It builds one wheel
   and sdist, inspects metadata and typed-package contents, and separately
   installs both exact artifacts using isolated Python outside the checkout.
   The rendered site and distributions are uploaded under run/attempt-qualified
   names. `Seal tested delivery` depends on every check, including each matrix
   result. PR runs still test/build but cannot seal deliverable material.
2. The seal records schema version 2, repository name/numeric ID, workflow
   numeric ID/path, run ID/attempt, source commit, exact job IDs, project version,
   the release notes, artifact IDs/names, ZIP SHA256 and every extracted file
   SHA256. Dist/site IDs and ZIP digests must match the uploader action outputs
   passed through job outputs, not merely a matching name or creation timestamp.
   The seal and receipt reject version 1 documents; there is no dual-accept path.
   The notes are the body of the `## <version>` section of `CHANGELOG.md` read
   from the sealed checkout, the same root as `pyproject.toml`. The heading line
   must equal `## ` plus the project version by string comparison, never a
   pattern, so `0.7.0rc1` does not match `0.7.0rc10` or `0.7.0rc1 (draft)`. The
   section runs to the next level-2 heading or the end of the file, keeps its
   `###` subheadings, and excludes the heading and surrounding blank lines. Line
   endings are folded to LF. The file is decoded as strict UTF-8, and a missing,
   empty or duplicated section fails the seal. Lines inside top-level fenced code
   blocks (CommonMark: 0-3 spaces of indent, three or more backticks or tildes,
   a closer of the same character and at least the same length) are never
   headings, duplicates or section ends; the fence scan starts at the first
   line of the file, and an unclosed fence anywhere fails with its own reason.
   Fences nested in lists or block quotes are not parsed. The same check runs
   without GitHub access as `python -m scripts.delivery notes`, which the
   `Validate release notes` job runs on every pull request and push.
3. `python-publish.yml` runs from the trusted default-branch workflow revision.
   It revalidates the successful same-repository `push` to `main`, workflow ID,
   attempt endpoint and full job set, and queries authoritative associated PRs
   and pull details for the exact merged-main SHA. It downloads by artifact ID,
   explicitly verifies each ZIP digest before inspecting data, and checks every
   file against the seal. It never checks out producer code or installs/builds
   downloaded distributions in the privileged consumer.
4. The publisher queries `https://pypi.org/pypi/pywhales/{version}/json`.
   Existing wheel/sdist files must have the exact filenames, package types and
   SHA256 hashes. Only missing files are offered to the uploader. A failed,
   skipped or concurrent upload can yield a receipt only if a fresh destination
   inventory confirms both exact distributions. Unexpected/conflicting files
   fail closed. A matching import or version string is not publication evidence.
   If files are missing, `confirm` rechecks the full inventory up to 12 times,
   five seconds apart, to allow for PyPI visibility delays. Conflicting,
   invalid or malformed responses still fail immediately. Exhaustion reports
   the filenames still missing and does not issue a receipt.
5. The receipt embeds the complete seal, seal-artifact identity/content hashes,
   publisher identity and explicit destination confirmation. A successful
   publisher run with no receipt, or a skipped publishing job, cannot release.
   The receipt is uploaded under the publisher run/attempt-qualified name.
6. `release-on-merge.yml` consumes only a successful publisher receipt. It
   independently repeats producer and destination validation. Tag creation,
   release creation and `422` races are accepted only after resolving the
   existing lightweight or annotated tag to the receipt's exact source commit.
   New releases are created as drafts and the tag is resolved again before
   publication, so a tag creation/mutation race cannot be accepted by name.
   Draft and prerelease records are not accepted as the published release.
   The release body is the sealed notes; `generate_release_notes` is never sent
   and `release` never reads `CHANGELOG.md`, which belongs to another checkout.
   An existing or just-created draft gets its body and `draft: false` in one
   PATCH, sent only after the tag-to-commit and prerelease checks pass. A
   published release whose body differs is rejected without any PATCH. Bodies
   are compared after folding CRLF/CR to LF and removing trailing whitespace
   from every line and from the end. Finally the release is read back and its
   body compared the same way. `released`, which Pages also uses, does not
   compare bodies, so Pages redeploys never depend on release text.
7. Release completion explicitly calls `gh-pages.yml` using `workflow_call`.
   It does not depend on a token-created release event triggering anything.
   Pages validates the same receipt, destination and release, then extracts the
   tested site without regeneration. Pinned upload-pages-artifact v5 preserves
   hidden files and repackages the validated regular files into the current
   deploying run, as required by deploy-pages. The actual generated Pages tar is
   compared against every tested site file before deployment is permitted.
   Manual `workflow_dispatch` requires
   `publisher_run_id` and invokes precisely the same checks. Non-main dispatch
   refs cannot enter the privileged job.

## Fail-Closed Reruns And Retention

GitHub artifacts are run-scoped, not attempt-scoped. Attempt-qualified names,
uploader IDs/digests and the complete attempt-specific jobs endpoint are all
required. Artifacts must also have matching authoritative run/repository/source
linkage and creation/update times inside their producing job interval. GitHub
does not expose an artifact job ID; timestamps alone are not treated as proof.
Job `run_attempt`, when returned by GitHub, must match the scoped endpoint.

Missing, expired, duplicate, unsafe, altered or mismatched evidence is an error.
Every pagination endpoint is exhausted, within the caps below, before selecting
a candidate. A newer
producer/publisher attempt invalidates an older receipt unless that entire
latest chain is complete and matching. Rerunning only failed jobs cannot borrow
checks or material from a previous attempt. Use a full workflow rerun/fresh run
when that proof is missing. Artifact retention is GitHub's repository default;
expired artifacts require a new complete tested chain, not a rebuild downstream.

The publisher serializes publication, and Pages serializes deployments, without
cancelling an in-progress delivery. Confirmation allows up to 60 seconds of
polling waits, in addition to request time. If visibility takes longer, retry
after PyPI's inventory exposes successfully uploaded files. No credentials are
passed to downloaded artifacts, and privileged checkouts always use
`github.workflow_sha`.

## Executable Local Checks

### Finite input budgets

All sizes below are binary MiB (1,048,576 bytes). Policies live in `limits.py`;
they are fixed trusted-tool limits, not producer-controlled metadata or CLI
overrides.

| Independent boundary | Input bytes | Entries (including directories) | Expanded bytes/member | Expanded bytes total |
| --- | ---: | ---: | ---: | ---: |
| HTTPS response / downloaded artifact ZIP | 64 MiB | 8,192 per ZIP | 16 MiB | 128 MiB |
| Wheel and sdist, each (including local smoke) | 16 MiB | 4,096 each | 8 MiB | 64 MiB each |
| Standalone Pages tar | 160 MiB | 8,192 | 16 MiB | 128 MiB |
| Local Pages reference site | Files read individually | 8,192 | 16 MiB | 128 MiB |
| Local project configuration (smoke and seal) | 1 MiB | — | — | — |
| Local `CHANGELOG.md` (seal) | 1 MiB | — | — | — |
| Sealed release notes section (UTF-8) | 120,000 | — | — | — |

PyWhales distributes pure Python, type markers and metadata, not native binaries
or bundled datasets. A 16 MiB compressed distribution and 64 MiB expanded
distribution leave substantial room for sources and tests. Static MkDocs output
gets twice that aggregate allowance and 8,192 files/directories for API pages,
search data and assets. The 160 MiB Pages wrapper accommodates 128 MiB of site
payload plus tar headers, padding and path metadata. The 64 MiB network allowance
also accommodates the pair of compressed distributions or a compressed site ZIP;
ordinary paginated REST JSON is much smaller. Larger legitimate deliverables
require an explicit review of these trusted policies, not silently unbounded
fallbacks. The separate 1 MiB project-configuration cap leaves room for extensive
tool configuration and UTF-8 comments while bounding both independent local
`pyproject.toml` reads before TOML decoding. The whole `CHANGELOG.md` read has
the same 1 MiB cap, which holds many releases of history. The notes cap applies
to one section after the strict UTF-8 decode and is measured in encoded bytes.
GitHub rejects release bodies above 125,000 characters. Bytes are never fewer
than characters, so 120,000 bytes also bounds the characters and leaves 5,000
of headroom.

### Bounded pagination

`GitHub.pages` in `api.py` is the only list paginator; job, artifact, release
and commit-pull lookups all use it with the exact `?per_page=100&page=N` query.
GitHub documents no page depth for these endpoints, so the item caps in
`limits.py` are not GitHub limits. Each is derived from what this repository's
workflows produce, with headroom:

| Endpoint | Item cap | Requests allowed | Rationale |
| --- | ---: | ---: | --- |
| Attempt jobs | 200 | 3 | One producer attempt has 26 jobs (nine fixed checks, the 16-entry component matrix, the seal); the publisher has one. |
| Run artifacts | 100 | 2 | Run-scoped across attempts; a producer attempt uploads three and a publisher attempt one, so over 30 full reruns fit. |
| Releases | 500 | 6 | One release per published version, scanned for an authenticated draft. |
| Commit pulls | 100 | 2 | A merged-main commit is associated with one merging pull request and rarely more. |

A listing may make `cap // 100 + 1` requests, which leaves room for the empty
terminator after a final full page. A short page ends the listing; a count never
does. The paginator rejects too many items, more than 16 MiB of response bodies
summed over all pages (beside the 64 MiB per-request budget), and a repeated
item identity (`id`, or `number` for commit pulls). Caps and byte totals are
inclusive: a value exactly at the limit passes and one past it raises `Rejected`
with a payload-free reason. A non-200 page fails instead of ending pagination.

Job and artifact envelopes must carry a non-negative integer `total_count` on
every page. It must not change between pages and must equal the final collected
count, so silent server truncation fails. The bare-array release and commit-pull
endpoints have no such field: truncation there remains undetectable, and only
the caps and duplicate checks apply.

Actual received/read bytes are counted in bounded chunks with a one-byte excess
probe before accumulation. Exact limits succeed; declared lengths and archive
sizes only permit early rejection, never exemption from actual accounting.
Curl's final four-byte status suffix has a separate framing allowance; stderr
goes directly to the null device (zero retained bytes). The existing 10-second
connect, 120-second curl and 130-second supervisor deadlines, HTTPS-only
redirects and host-scoped authorization remain in effect. Every aborted owned
curl is killed and reaped.

The ZIP digest is checked before any member is trusted. Nested wheel/sdist and
standalone tar/site checks have independent budgets; an outer ZIP limit is not
treated as a nested expansion limit. Only complete verified inventories reach
consumers, and site extraction is staged before publication into its fresh
destination. Rejection discards staging. These guarantees cover retained bodies
and member payloads, **not total parser/decompressor memory or CPU** or resources used by the producer-only isolated package installs.

```bash
uv run --locked pytest tests/pywhales/test_delivery_workflows.py
uv run --locked ruff check scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked ruff format --check scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked ty check --config 'src.include = ["scripts/delivery", "tests/pywhales/test_delivery_workflows.py"]' scripts/delivery tests/pywhales/test_delivery_workflows.py
uv run --locked python -m scripts.delivery --help
```

The tests drive the real helper decisions and CLI against stubbed REST,
artifact-download, PyPI and release boundaries. They do not pin YAML prose or
publish production packages. The parent CI still owns full SDK integration,
documentation, package builds, and destructive disposable-daemon coverage.

## API Contracts

- [Attempt-specific jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
- [Run-scoped artifact inventory and ZIP download](https://docs.github.com/en/rest/actions/artifacts)
- [PyPI release JSON file inventory](https://docs.pypi.org/api/json/)
- [Token-trigger restrictions](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow)
