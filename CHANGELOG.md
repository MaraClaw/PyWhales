# Changelog

## 1.1.6

### New Features

- `AdmissionOwner(owner_pending_demand_capacity=...)` optionally bounds queued
  declared demand across all of its pressure classes. Exact fits are accepted;
  zero still permits immediate grants. Rejection uses
  `owner_pending_demand_full` and an appended saturating outcome counter, without
  invoking the operation or stream factory. The default remains unbounded.
- `capture_compose()` and `capture_containers()` accept default-off
  `fail_on_unhealthy=True`. An explicitly unhealthy long-running member fails
  only while running, unpaused, not restarting, and within its enabled restart
  budget. Completion members ignore health. Both observation passes can return
  cause-free `Failure(reason="unhealthy")`; reconciliation records `FAILED` with
  that closed reason and clears deadline detail without retaining health data.
- Container `create()` and all `run()` modes and root aliases accept
  `health_start_interval`: positive whole-second integers or integral-second
  `timedelta` values, validated before image preflight. Create profiles accept
  positive exact integer seconds, require an explicit
  `SecurityProfile.max_health_start_interval` ceiling, and require the selected
  runtime to declare the control only when requested. This option requires an
  operator-qualified Docker runtime supporting API 1.44+; no version probes or
  fallback are added.
- `InspectionPolicy.max_concurrent_children` optionally bounds simultaneously
  owned local CLI children across captured calls, streams, and image archive
  transfers. Nested scopes and copied contexts share atomic allowances.
  Saturation raises `ObservationLimitError` with reason `concurrent_children`
  without spawning, reading upload input, or consuming a launch attempt. Slots
  release when children are reaped, even with buffered output still undrained;
  accepted spawn failures retain their cumulative attempt charge. The default
  remains unbounded, and the limit does not count daemon workloads.

### Behavior Changes And Fixes

- All telemetry record kinds now emit schema version 8. Admission decisions add
  the closed `owner_pending_demand_full` outcome; existing field meanings,
  correlation, payload-free records, and unlocked observer delivery remain intact.

### Documentation

- Updated API documentation, relevant agent guides, and the consumer skill for
  shared queued demand, unhealthy convergence, approved startup health intervals,
  concurrent local children, and schema 8 telemetry.

## 1.1.5

### New Features

- Admission `ClassLimit.pending_demand_capacity` optionally bounds queued declared
  demand separately from pending request count and active demand. Exact fits are
  accepted; zero still permits immediate grants. Queue-demand rejection never
  invokes the operation or stream factory and has a distinct `pending_demand_full`
  reason and saturating snapshot counter. The default remains unbounded.
- `InspectionPolicy.max_total_launch_attempts` bounds accepted CLI launch
  reservations across enclosing observation scopes and copied contexts. Captured
  calls, streams, archive loads and lazy saves share atomic accounting; accepted
  reservations are not refunded after spawn or CLI failures. Inventory checks its
  complete inspection plan against remaining allowances before its first inspect,
  without reserving the whole plan. Exhaustion raises `ObservationLimitError` with
  reason `total_launch_attempts`; the default remains unbounded.
- `capture_nodes(require_leader=True)` freezes an optional leadership requirement
  into `NodeTarget`. A nonempty designated-manager subset must observe exactly one
  leader on both complete clear passes, allowing the leader identity to change.
  Omitted leader fields mean false; applicable explicit nonboolean values produce
  cause-free `malformed` outcomes. Bounded deadline detail includes
  `leader_mismatch_managers`, with reconciliation round-trip support. Checks remain
  read-only, sequential and limited to selected managers, not global leadership.
- Scheduled offline fleet steps accept fixed positive finite
  `queue_retry_backoff_seconds` with a queue lease and multiple allowed attempts.
  Backoff occupies neither queued nor active capacity, retains the original
  deadline, and is cancelled immediately on early stop while active work drains.
  Launched work is never retried. Explicit intervals preserve retry history;
  backoff schedules use local version 2 while binding version stays 1. Unset
  options preserve legacy report bytes and operation digests.

### Behavior Changes And Fixes

- All telemetry record kinds now emit schema version 7. The closed admission
  outcome vocabulary adds `pending_demand_full`; decisions remain payload-free,
  delivered outside owner locks, with no added observer overhead when disabled.
- Imported fleet histories reject unexplained retry gaps, invalid backoff or
  deadline arithmetic, false peaks, and pending work that survives an established
  early stop. Supplied backoff values bind operation digests and comparison scope.
  Reports remain offline, unqualified, and non-authorizing with zero connections
  and unchanged `no_go` decisions.

### Documentation

- Updated API documentation, agent guides, and the consumer skill for queued
  demand, shared launch reservations, schema 7, selected-manager leadership, and
  offline fleet retry contracts.

## 1.1.4

### New Features

- `capture_nodes()` and the frozen `NodeTarget` track exact, fixed Swarm node
  IDs and an explicit manager subset. Selected nodes must be ready and active,
  and designated managers must retain their role and be reachable. Success
  requires two complete all-clear passes; nodes missing after capture return
  `superseded`. Node readiness has no version fence.
- Telemetry schema 6 adds payload-free `RefusalRecord` events for preflight
  cancellation, timeout, or limit refusal. Each refusal is recorded exactly once
  without a launch or completion; earlier validation is outside this boundary,
  and lazy save preflight occurs on first iteration.

### Behavior Changes And Fixes

- Container, image, network, volume, and system prune accept keyword-only
  `timeout`, including container prune's streaming branch. `None` inherits the
  client default; an explicit timeout overrides it.
  Expiry affects only the local CLI child and does not roll back daemon work.
- Completed and rejected fleet reports bind the full normalized manifest and
  retained sanitized environment with SHA-256 digests. Imports reject tampering;
  legacy unbound reports remain readable but cannot be exported or matched.
  Baseline comparisons include the environment digest. The harness remains
  offline and its `no_go` decision is unchanged.

### Documentation

- Updated documentation, agent guides, and the consumer skill for the new
  convergence, prune deadline, telemetry, and fleet report contracts.

## 1.1.3

### New Features

- `capture_compose()` and `capture_containers()` accept `fail_on_exit=True`,
  frozen as a real boolean on their targets and disabled by default. A
  non-restarting long-running container that exits with any code returns a
  cause-free `Failure(reason="exited")` on the first complete or confirming
  pass. Completion, restarting, and restart-budget behavior remain unchanged.
  Reconciliation journals this as `FAILED` with reason `exited`, without output
  or exit code.
- `AdmissionOwner(telemetry_observer=...)` optionally emits a payload-free
  decision ID, outcome, and activation-to-decision duration in seconds. Each
  activated request produces one decision; dormant cancellation produces none,
  and unobserved requests incur no telemetry overhead. IDs correlate calls and
  stream factory, entry, iteration, and exit; observers run outside locks.
- Scheduled offline fleet steps support `queue_lease_seconds` and
  `max_queue_attempts` for bounded FIFO tail retries under the original
  deadline. Launched work is never retried; receipts include every attempt but
  retain one logical outcome and sample. Unset options preserve legacy digests
  and reports. The harness remains offline and non-authorizing.

### Behavior Changes And Fixes

- `strict_workload_errors=True` opts into exact `DockerException` classification
  for marked attached container `run`, `execute`, and `start`, and non-detached
  Compose `run` and `execute` workload failures, including captured, TTY, and
  supported stream paths. It defaults to `False`; exception attributes, detached
  and preflight classification, timeouts, cancellation, and limits are unchanged.
- Telemetry uses schema version 5 for admission decision, launch, and completion
  records. `AdmissionDecisionRecord` joins `TelemetryRecord`, `LaunchRecord`
  carries `admission_id`, and completion records join launches by `attempt_id`.

### Documentation

- Updated agent guides and the consumer skill for the new convergence, admission
  telemetry, offline fleet, and workload-error contracts.

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
