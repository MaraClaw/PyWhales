# PyWhales Agent Guide

**Verified:** 2026-11-14 against source version 1.3.0 for Docker CLI 29.9.3, Buildx v0.39.0 and Compose v5.7.1 command coverage; 2026-11-10 against 1.2.1 for inherited environment selection and archive stderr allowances; the four completed 1.2.0 proposals were checked 2026-11-08, and other guidance 2026-11-01.

## Overview

PyWhales is a typed Python wrapper around the external Docker CLI, not a Docker HTTP SDK.
Python >=3.14.10 and Pydantic 2 are required; `.python-version` pins 3.14.10.
The public API exports `DockerClient`, resource classes, a shared `docker` client, and the inventory and observation entry points from `pywhales/__init__.py`.
`DockerClient` wires 18 command groups to one `ClientConfig` and exposes convenience aliases such as `run`, `ps`, `build`, `events`, `port`, `history`, and `search`. Command coverage targets Docker CLI 29.9.3; the removed `trust` group isn't wrapped.
Importing the package or constructing a client doesn't check CLI, daemon, or plugin availability.
`DockerClient(inherited_environment_names=...)` selects exact ambient names before explicit overlays. `None` preserves `inherit_environment` behavior; an empty collection inherits nothing. This doesn't confine executable lookup, configuration files, or credential helpers; see the runtime guide.
The installed `pywhales` console entry point only prints a notice; it isn't a Docker command runner.
Container, image, network, volume, node, service, config, and secret `list(hydrate=True)` batch-inspect and pin returned snapshots; default listings remain lazy. Ordinary cache expiry uses monotonic time, and copying or unpickling clears freshness and pins.

Bounded observation sits on top of the command groups:

- `observation()` nests explicit limits, deadlines, and cancellation over launches, including shared optional `max_total_launch_attempts` and `max_concurrent_children` reservations. Child slots last until local CLI reap, not output drain or daemon-workload completion. Inventory checks its complete launch plan against remaining attempt allowances without reserving it. `capture_inventory()` returns a typed `InventorySnapshot` of detached inspect records for 13 kinds: container, image, network, volume, node, service, config, secret, task, plugin, context, builder, and manifest_list.
- `InspectionPolicy.max_total_io_bytes` adds an optional shared total over actual captured, stream and archive output reads plus managed stdin writes. Nested and copied contexts share atomic accounts; source budgets still apply. See the runtime guide for detection-byte, EOF and input boundaries.
- `InspectionPolicy.max_total_archive_stderr_bytes` separately bounds actual direct-archive stderr reads, including after local reap. Opting in drains load output during upload and save stderr while consumption is paused; nested/copied accounts and launch-retained cleanup follow the runtime and image guides. It isn't a total-memory bound.
- Multi-image pull/push fanout fits the observation's remaining child slots, capped at four. Sizing doesn't reserve capacity or change immediate launch refusals; see the image guide.
- `pywhales.convergence.wait()` watches a captured Swarm service generation, fixed stack membership, selected Swarm nodes, selected Compose services, or standalone containers. Node readiness uses fixed IDs and an explicit manager subset, not version fencing; `capture_nodes(require_leader=True)` additionally requires exactly one observed designated leader on both clear passes. It ends in success, failure, deadline, or cancellation and never mutates anything. Compose and container captures optionally enable `fail_on_exit` and `fail_on_unhealthy`; see the convergence guide.
- Convergence task, container, node and stack-service inspections use policy-sized batches with ordered single-ID fallback; service batches never mix client configs. Scalar service capture and standalone reference resolution stay single-call; failure classifications, `NodeMissing` and complete-cycle publication are preserved.
- Reconciliation convergence adapters may renew their lease through `ActionContext.renew_lease(ttl)` between short waits; nothing renews in the background.
- `admission`, `convergence`, `profiles`, `reconciliation`, and `storage` are opt-in application packages. Import them explicitly: `pywhales/__init__.py` doesn't re-export them, and direct `DockerClient` calls stay permissive without them.
- Admission `AdmissionOwner(..., owner_demand_capacity=...)` bounds active declared demand across classes for immediate/queued grants and resize growth. Separate `ClassLimit.pending_demand_capacity` and `owner_pending_demand_capacity` bound queued demand; immediate grants bypass those queue-only bounds. See the admission guide for growth guards and `owner_demand_full`.
- `telemetry` optionally records payload-free admission decisions, launches, completions, and preflight refusals under schema 11. Completion/refusal `stop_reason` uses a closed vocabulary, including `total_io`, `buffer_output_record` and `buffer_stderr_history`; `BoundedExporter.stop_reason_counts` counts every offer, including dropped records. Launches and refusals can correlate with admission decisions; refusals never fabricate a launch or completion. No monitoring dependency or worker is required.
- `strict_workload_errors` is an opt-in client setting for marked attached workload failures; see the shared runtime and container/Compose guides.

## Structure And Navigation

| Path | Start here for |
| --- | --- |
| `pywhales/docker_client.py` | Public client, command-group wiring, top-level aliases |
| `pywhales/client_config.py` | Shared client options, command assembly, reloadable resource bases |
| `pywhales/_execution_policy.py` | Originating-config carrier and per-launch execution policy |
| `pywhales/_inspect_validation.py` | Context, plugin, and task batch-response validation |
| `pywhales/_bulk_reload.py` | Grouped inspection and staged resource-cache publication |
| `pywhales/components/_hydration.py` | Chunked listing hydration, exact identity matching, vanished-resource re-probes, and handle pin release |
| `pywhales/inventory.py`, `pywhales/_inventory_*.py` | Inventory kinds and value types, lazy capture engine, kind dispatch, ordered staged inspection |
| `pywhales/_observation.py` | Context-local limits, deadlines, and cancellation checkpoints |
| `pywhales/_bounded_capture.py`, `pywhales/_scoped_launch.py`, `pywhales/_launch_limits.py` | Bounded interruptible capture and launch preflight; related helpers are `_capture_pipes.py`, `_readiness.py`, `_archive_deadline.py`, `_stdin_pump.py`, `_environment.py` |
| `pywhales/utils.py` | Subprocess execution, common conversions, Pydantic model base |
| `pywhales/_process_stream.py` | Process-backed streaming and lifecycle ownership |
| `pywhales/_reclaim.py` | Shared reclaimer that closes stream cores after their public handles are collected |
| `pywhales/_stream_buffer.py` | Thread-safe output buffering and byte accounting |
| `pywhales/_environment.py` | Effective child environments and safe container `envs` forwarding |
| `pywhales/telemetry/` | Admission decision, launch, completion and refusal records plus the bounded exporter; no child guide, covered by `pywhales/AGENTS.md` |
| `pywhales/convergence/` | Swarm service/node, Compose, and standalone container convergence: `_wait.py` entry points, `_service.py`, `_node.py`, `_compose.py`, `_container.py`, `_wire.py`, `_types.py` |
| `pywhales/admission/` | Application-owned synchronous admission: class limits, saturation policy, queueing, admitted streams |
| `pywhales/profiles/` | Application create policy, including startup health-interval approval: `plan_create` builds a plan, `create_from_plan` applies it |
| `pywhales/reconciliation/` | Desired-state controller, operation journal, adapters; only an in-memory store ships |
| `pywhales/storage/` | Non-destructive retention planning, immutable approvals, pressure assessment and gate |
| `pywhales/components/` | Docker command groups; start with each group's `cli_wrapper.py` and `models.py` where present |
| `tests/` | Pytest suite, shared runtime fixtures, static type contracts |
| `docs/` | Editable templates, API-doc generation, MkDocs configuration |
| `scripts/` | Contributor tooling, not installed APIs: CI provisioning, plugin installation, overhead benchmark |
| `scripts/fleet_contract/` | Offline fleet contract: strict manifest, support, and result schemas, validators, non-qualifying reports (see its `README.md`) |
| `scripts/fleet_harness/` | Fake-only offline capacity simulation, including leased retries with fixed or capped exponential delays, and non-authorizing transport decisions; see its guide |
| `scripts/delivery/` | Tested CI delivery chain: smoke, seal, publish, release, and Pages helpers; bounded GET retries and one command deadline |
| `skills/pywhales-docker-container/` | Consumer skill for using the installed library, not a contributor workflow |
| `.github/workflows/` | Authoritative CI checks, docs/site artifact build, and the delivery chain from package build to release; no Pages deployment workflow is present |
| `pyproject.toml`, `uv.lock`, `.python-version` | Project/tool configuration, locked dependencies, interpreter pin |

## Code Map

| Symbol | File | Role |
| --- | --- | --- |
| `DockerClient` | `pywhales/docker_client.py` | Public facade and shared-config command-group wiring |
| `ClientConfig` | `pywhales/client_config.py` | Dataclass holding CLI, connection, Compose, and execution settings |
| `Command` | `pywhales/client_config.py` | Argument-list builder with option/flag helpers |
| `ReloadableObject` | `pywhales/client_config.py` | Resource identity and refreshed inspection data |
| `DockerCamelModel` | `pywhales/utils.py` | Pydantic base mapping Python names to Docker JSON aliases |
| `ProcessStream` | `pywhales/_process_stream.py` | Iterator owning a CLI process and streamed output |
| `observation` | `pywhales/_observation.py` | Context manager nesting limits, deadline, and cancel tokens |
| `capture_inventory` | `pywhales/inventory.py` | Public entry that delegates to `_inventory_capture.py` |
| `wait` | `pywhales/convergence/_wait.py` | Convergence wait; `capture_service`, `capture_nodes`, `capture_compose`, and `capture_containers` build its targets |

Code-map discovery uses graph symbols, LSP outlines/references, and AST import searches. Editor references also match ignored `build/lib/pywhales` copies and are capped: don't quote them as exact source-only counts. Prefer graph tracing, or exclude `build/`, `dist/`, and `.venv/`.

## Guide Scope

- Read this root guide first, then the nearest child `AGENTS.md` for files you're changing.
- `pywhales/AGENTS.md`: shared runtime internals, observation and inventory capture, and telemetry.
- `pywhales/components/AGENTS.md`: command-group patterns. Buildx, Compose, container, image, manifest, and service directories have their own guides; Buildx also covers imagetools.
- Opt-in packages: `pywhales/admission/AGENTS.md`, `pywhales/convergence/AGENTS.md`, `pywhales/profiles/AGENTS.md`, `pywhales/reconciliation/AGENTS.md`, and `pywhales/storage/AGENTS.md`.
- `scripts/AGENTS.md`: tooling and provisioning hazards. Delivery, fleet contract, and fleet harness have child guides; `scripts/fleet_contract/README.md` remains authoritative for contract commands and semantics.
- `tests/AGENTS.md`: test selection and fixture details; `tests/pywhales/components/AGENTS.md` adds mixed component-test selection. `docs/AGENTS.md`: generation mechanics and the documentation pipeline.

## Commands From CI

Run from the repository root. These commands mirror `.github/workflows/python-package.yml` and `python-publish.yml`.
The strict coverage command is an integration run, not a safe default for a workstation daemon.

| Purpose | Command |
| --- | --- |
| Install locked development environment | `uv sync --locked` |
| Lint | `uv run --locked ruff check ./` |
| Check formatting | `uv run --locked ruff format --check ./` |
| Check types and static contracts | `uv run --locked ty check` |
| Full strict suite with coverage, disposable daemon only | `uv run --locked coverage run -m pytest -vv -ra --no-runtime-skip --durations=10` |
| Enforce coverage floor | `uv run --locked coverage report` |
| Generate documentation sources | `uv run --locked python -m docs.autogen` |
| Build documentation site | `uv run --locked mkdocs build --config-file docs/mkdocs.yml` |
| Build source and wheel distributions | `uv build --no-sources --sdist --wheel` |

After the build, CI runs `python -m scripts.delivery smoke`. The seal, prepare, confirm, release, and pages subcommands are privileged GitHub and PyPI steps; don't run them locally.

## Conventions And Checks

- Follow [the Python docstring standard](docs/python-docstrings.md) for new or changed Python module, class, function, and method docstrings; do not rewrite unrelated docstrings.
- Treat Ruff and ty settings in `pyproject.toml` as authoritative, not legacy `.flake8` or `.isort.cfg` files.
- Ruff targets Python 3.14 and handles import sorting as well as linting and formatting. Its checks cover `scripts/` too.
- ty checks `pywhales`, `tests`, and `docs`; all rules are errors and warnings fail the check.
- Root ty excludes `scripts/`, including `scripts/delivery`. Use the scoped ty commands in `scripts/delivery/AGENTS.md` and `scripts/fleet_contract/README.md`.
- ty doesn't respect type-ignore suppression comments; fix types rather than adding those comments.
- Static `assert_type` contracts in `tests/pywhales/test_type_contracts.py` and related files are checked by ty, not exercised by pytest.
- Coverage is measured for `pywhales` with a 90% minimum; `coverage report` enforces it.
- Keep dependency changes consistent with `uv.lock` and the interpreter pin; normal checks use `--locked`.
- A version bump needs a matching `## <version>` section in `CHANGELOG.md`: the delivery seal reads it as the release notes and rejects a missing, empty, or duplicate section.
- Public names of the opt-in packages come from each package's `__init__.py`; underscore modules are internal.
- Follow existing CLI argument and return-type contracts; check convenience aliases when changing component APIs.

## Safety And Generated Files

- The integration suite is destructive: use a disposable Docker daemon, not one holding valuable containers, images, volumes, or Swarm state.
- `--no-runtime-skip` only affects the session `docker_client` fixture in `tests/conftest.py`: a missing executable, failed version probe, or timeout fails instead of skipping. CI passes it, but it doesn't gate separately built or global clients, plugins, or the collection-time `compose.is_installed()` skip in `tests/pywhales/components/test_compose.py`, which skips independently. See `tests/AGENTS.md`.
- `scripts/ci-setup.sh` calls `scripts/add-local-docker-registry.sh`, which overwrites `/etc/docker/daemon.json` and restarts Docker with sudo.
- That setup also installs plugins; it's CI provisioning, not safe workstation setup. Don't run it casually.
- `scripts/download-docker-plugins.sh` downloads checksum-pinned Linux amd64 or arm64 plugins into `$DOCKER_CONFIG/cli-plugins` (default `~/.docker/cli-plugins`). Treat it as CI provisioning too.
- Don't run publication, release, or deployment operations of `scripts.delivery` against the production repository. Local delivery tests use inert stubs.
- `scripts/fleet_contract` and `scripts/fleet_harness` are offline: no Docker, daemon, or network. Their output never qualifies or authorizes a live run, so keep credentials and private endpoints out of their input files.
- `PYWHALES_DEBUG=1` enables execution diagnostics that withhold command, environment, and captured-output values by default.
  `DockerClient(raw_diagnostics=True)` opts that client into raw diagnostics; see `pywhales/AGENTS.md` for the confidentiality boundary.
- Documentation generation is Docker-free by default. `--live-examples` explicitly opts into real Docker operations.
- Edit `docs/template/` and source docstrings, not generated documentation output.
- `docs/generated_sources/`, `site/`, and `docs/site/` are ignored outputs; don't commit generated sources or site files.
- Keep build output, caches, virtual environments, and package metadata out of source changes.
