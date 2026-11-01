# Changelog

## 1.1.2

### New Features

- `capture_containers()` and the frozen `ContainerTarget` add standalone
  containers to `pywhales.convergence.wait()`, without requiring Compose labels.
  Capture resolves distinct names or ID prefixes to fixed full container IDs;
  missing or replaced containers later return `superseded`. Long-running
  containers must be running, unpaused, not restarting, and healthy when they
  have a healthcheck. Selected completion containers must exit 0; a nonzero exit
  that is not restarting returns `completion_failed`. Optional restart budgets,
  cancellation, bounded deadline details, and the final all-clear recheck follow
  the Compose rules. Observation never mutates Docker state.
- `DockerClient(inherit_environment=False)` starts CLI children with only the
  parent's available `PATH` and `HOME`, plus `USERPROFILE` and `SYSTEMROOT` on
  Windows, before applying explicit environment overlays. Docker endpoint,
  credential, proxy, and SSH variables must be passed explicitly. The default
  remains `True`, and executable lookup still uses the process `PATH`. Isolation
  covers captured commands, streams, archives, and staged inventory capture;
  container environment collision checks use the same effective child
  environment. This does not isolate files or credential helpers in the retained
  home directory.

### Behavior Changes And Fixes

- Scalar inspect hooks for container, image, network, volume, config, secret,
  node, service, task, plugin, and context handles require exactly one JSON
  object in a single-element array. Invalid JSON and malformed response shapes
  raise `ParsingError`; safe diagnostics suppress payload-bearing exception
  chains. Reload rejects a snapshot whose immutable identity differs from the
  handle, leaving its cached snapshot and refresh time unchanged. Handles that
  track the current context skip this identity check. Batch inspection and the
  distinct builder and manifest-list response shapes retain their contracts.
- The offline fleet harness enforces each action's declared concurrency cap
  alongside the global limit for overlapping submissions. Admission remains
  strictly FIFO: a capped head blocks later actions, while queued deadlines
  continue to expire. Schedule receipts record actions, reports expose canonical
  per-action active peaks, and imports validate those peaks, action bindings,
  and declared caps. Sequential execution and operation digests stay unchanged;
  the harness remains fake-only and offline.

### Documentation

- Updated API documentation, hierarchical agent guides, and the consumer skill
  for standalone convergence, child environment inheritance, strict scalar
  snapshots, and offline fleet scheduling contracts.

## 1.1.0

### New Features

- `InspectionPolicy.max_total_stdin_bytes` bounds bytes accepted by local stdin
  pipe writes across captured input and image archive uploads. Nested scopes
  share accounting across their launches, including partial writes and transfers
  retained after scope exit. Exact-budget EOF succeeds; excess input raises
  `ObservationLimitError` with reason `total_stdin`. Budgeted captured input
  supports bytes and cooperative byte iterables while draining stdout and stderr.
- `capture_compose(max_restarts=...)` optionally requires each selected
  long-running container's current `RestartCount` to stay within a nonnegative
  budget. Over-budget services remain unsettled even when healthy; a later
  within-budget observation clears the blocker. Completion services are exempt.
  `DeadlineDetail.restart_budget_exceeded_containers` counts affected services
  from the latest complete cycle, and success still requires the final recheck.
- Swarm convergence supports `ReplicatedJob` and `GlobalJob` targets, tracking
  completion within the captured job iteration. Global jobs require explicit
  approved node IDs. Failed or rejected tasks with restart condition `none`
  return `completion_failed`; a new iteration or changed template supersedes
  the capture, while a service version change alone does not.
- `DockerClient(strict_env_transport=True)` rejects container environment and
  build-argument assignments that would otherwise fall back to literal CLI
  arguments. The default remains permissive. Rejections omit assignment names
  and values; this does not hide child environments, image history, or provenance.
- The offline fleet harness accepts explicit arrival offsets for deterministic
  overlapping submissions, FIFO admission, queue-inclusive deadlines, and
  chronological resource lifecycle simulation. Reports include validated
  per-submission schedule receipts and observed active/queued peaks. Plans
  without offsets retain sequential behavior and operation digests.
- The fleet harness exports synthetic evidence for the fleet contract reporter.
  Exported results remain unqualified, establish zero external connections,
  and never authorize a live run or a transport beyond `no_go` and `cli`.

### Behavior Changes And Fixes

- Eligible Buildx and Compose build arguments use name-only `--build-arg KEY`
  options with values in the child environment, retaining literal fallback by
  default. Compose environment values also reach configuration interpolation;
  Bake `--set` transport is unchanged.
- Compose `build` and `execute`, and container `wait` (including `docker.wait`),
  accept keyword-only local CLI deadlines. Container/service logs and system
  events reject invalid deadlines before inspection or filter processing.
  `None` inherits the client default; expiry reaps only the local CLI and does
  not roll back daemon-side work or change Docker shutdown-grace options.
- Fleet contract samples require a submission identity linking observations
  across boundaries. Assessments flag conflicting linked outcomes and aggregate
  count mismatches. Existing schema-version-1 result files without these links
  must be migrated; the shared schema version remains unchanged.
- Integration tests identify annotated manifest members by digest, wait for
  ARM64-compatible stack services to converge, and explicitly refresh service
  snapshots after scaling instead of relying on inspection-cache timing.

### Release Process And Documentation

- The Linux plugin installer supports amd64 and arm64 with architecture-specific,
  checked-in checksums, verifying both pinned plugins before replacing either.
  Delivery now requires native ARM64 Compose and Buildx smoke tests in both the
  workflow dependencies and the seal's exact successful-job contract.
- Updated API documentation and consumer skill guidance for deadlines,
  environment transport, Swarm jobs, bounded uploads, and Compose restart budgets.

## 1.0.8

### New Features

- `capture_compose()` accepts `completion_services` for selected one-shot
  services, which must exit successfully. A nonzero exit returns payload-free
  `Failure(reason="completion_failed")`; the `unfinished_completion_containers`
  counter counts affected services, not individual containers.
- Inventory capture and `bulk_reload()` support `manifest_list` resources.
  Each reference uses a singleton inspect; existing handles retain their
  `insecure` setting, and bulk reload stages cache updates until all inspections
  succeed.

### Behavior Changes

- Owned record stream return types expose `close()` and context management.
  An empty streaming image pull returns an empty context-managed stream whose
  close is idempotent, without looking up or launching the CLI.
- Eligible container, service, and Compose `envs` values move from CLI arguments
  to the child environment while preserving their values. Reserved or colliding
  names fall back to literal arguments. This is not universal secrecy: values
  may still appear in arguments or be exposed through the child environment.
  For Compose, forwarded values may also affect configuration interpolation.

### Documentation

- Updated the consumer skill guidance for the new APIs and their boundaries.

## 1.0.6

### New Features

- `InspectionPolicy.max_total_stream_bytes` optionally bounds combined stdout and
  stderr read by streams and image archive transfers in an observation scope.
  Nested scopes charge every enclosing stream budget independently of captured
  response budgets. Overflow stops the local CLI and raises
  `ObservationLimitError` with reason `total_stream`.
- Service create, update, rollback, and scale; Swarm CA and join; stack deploy;
  and plugin install and upgrade accept keyword-only local CLI timeouts. The
  service update/scale and plugin upgrade handles forward these deadlines.
  Preflight and postflight inspection remain outside the selected launch budget;
  each Swarm CA launch receives a fresh budget.

### Behavior Changes

- Buildx builder reload/list, Compose config/ls/events, system disk-free/events/info,
  and imagetools manifest parsing raise `pywhales.client_config.ParsingError` for
  malformed responses. This is not a `ValueError` subclass. Safe diagnostics
  suppress payload-bearing exception chains; `raw_diagnostics=True` chains the
  original parsing error. Event streams snapshot this setting at launch.
- Telemetry records use schema version 4. Observation cancellation reports
  `cancelled`, and observation-limit rejection after launch reports `limit`,
  including stream-byte budget overflow. Observation deadlines still report
  `timeout`; stream-buffer errors and `KeyboardInterrupt` remain `failure`.
  Prelaunch rejections emit no records.

### Internals

- Added an internal JSON utility with a native orjson pair and a standard-library
  compatible pair. Existing CLI decoders, canonical hashes, diagnostic files,
  delivery REST helpers, and the overhead benchmark use the compatible pair to
  preserve accepted inputs, error types, and serialized bytes.
  The fleet contract and harness, the isolated distribution smoke child, and
  test oracles stay on the standard library.

## 1.0.5

### New Features

- Capture selected Compose project services and wait for their containers to be
  running and healthy. Changed membership, service labels, or configuration
  hashes supersede the capture; a one-shot container that exits 0 remains a
  blocker.
- Inventory captures and bulk reloads support Buildx builders. Each builder
  group uses one strictly parsed listing, and cache updates publish only after
  all groups succeed.
- Image and Compose push, container start, Compose watch, and Buildx prune accept
  per-call timeouts for the local CLI. Each image transfer has its own deadline;
  the preceding image inspection and queueing are excluded. Timeout does not
  undo daemon or registry work.

### Release Process

- GitHub delivery pagination now enforces item and byte caps, rejects duplicate
  identities, and validates consistent `total_count` values for jobs and
  artifacts. Truncation of bare-array release and commit-pull listings remains
  undetectable.

## 1.0.0

### Behavior Changes

- Multi-image `pull()` and `push()` now behave like single-image transfers
  inside `observation()`: each pool task and stream worker runs in its own copy
  of the caller's context, so `pull(quiet=False)` in a scope raises the same
  `ValueError` for one image or many.
- Streams and archive transfers started inside `observation()` now honor its
  cancel tokens, deadline, and launch limits. When the scope ends first, the
  local CLI is killed and iteration raises `ObservationTimeout` or
  `ObservationCancelled`. Each transfer keeps the scope it launched with after
  the `with` block exits, and a lazy `save()` archive uses the scope active at
  its first iteration.
- Missing-resource errors are now classified only from the kind phrase that
  starts a stderr line, so a resource name or a file path no longer selects the
  exception type. For example, a missing network named `service-foo` now raises
  `NoSuchNetwork` instead of `NoSuchService`.

### New Features

- Add `NoSuchConfig`, `NoSuchSecret`, `NoSuchPlugin`, and `NoSuchContext`.
- Config, secret, plugin, and context handles gain `exists()`.
- Config, secret, plugin, and service context managers exit cleanly when their
  object was already removed; other cleanup failures still propagate.
- `config.list()`, `system.events()`, `system.prune()`, `buildx.prune()`, and
  `compose.ls()` accept an iterable of `(key, value)` filter pairs as well as a
  mapping, so a filter key can be repeated.

### Release Process

- GitHub release bodies are now the sealed `CHANGELOG.md` section for the
  released version instead of generated notes. A release fails to seal when that
  section is missing, empty, duplicated, or oversized.

## 0.6.0

### Breaking Changes

- `stack.deploy()` no longer accepts `orchestrator`; omit the argument.
- Use `client_call` instead of `client_binary`.
- Use `compose_env_files` instead of `compose_env_file`.
- Container, image, network, volume, service, and secret filters now accept an
  iterable of `(key, value)` tuples instead of a mapping.
- Set `PYWHALES_DEBUG=1` to enable execution diagnostics.
