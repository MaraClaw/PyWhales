# Convergence Contracts

Read-only observation of captured Swarm service/job generations, a fixed stack, selected Compose services, or standalone containers. Nothing here creates, updates, or rolls back daemon state. Observation scopes, launch limits, and local process cleanup are in [the shared runtime guide](../AGENTS.md).

## Local Navigation

- `__init__.py` re-exports the public names. `_wait.py` holds `capture_service`, `capture_compose`, `capture_containers`, `wait`, and the Swarm polling loop.
- `_types.py`: frozen targets and outcomes. `_wire.py`: strict Pydantic projections, inspect helpers, standalone reference resolution, and generation fences.
- `_service.py`: `check_service` and `ServiceObservation` for Swarm. `_container.py`: standalone capture, `check_containers`, and `wait_containers`.
- `_compose.py`: Compose capture and `check_compose`; shared `evaluate_groups`/`wait_groups` also serve standalone targets. `_pause` stays here, not in `_wait.py` or `_container.py`.

## Types And Outcomes

- Dataclasses are frozen and slotted. `client_config` is `repr=False, compare=False` on `ServiceTarget`, `ComposeTarget`, and `ContainerTarget`, as is `Failure.cause`; `template` and `approved_node_ids` leave `repr` but stay in equality. Preserve that distinction between client settings and captured generation data.
- `StackTarget` fixes a nonempty tuple of distinct service IDs. Global services/jobs require a nonempty tuple of distinct approved node IDs; replicated services/jobs take no approved nodes.
- `Deadline` accepts only a concrete `DeadlineDetail` (`type(...) is`), so a subclass can't smuggle a payload. Per-reason integer counts stay in `0..captured_services`, with no identities, tasks, containers, settings, or causes. Each standalone container is one `captured_services` unit; Compose counts affected services, once per reason even with multiple blocked containers.
- Detail is the latest complete cycle, never a partial one or a fresh inspection at expiry. It is absent when capture expired or no cycle finished.
- Swarm readiness is `ServiceObservation.ready`; grouped container success is `_settled`, an all-zero blocker `DeadlineDetail`. A new Swarm blocker needs a flag plus `summarize`; a new group counter blocks by default once populated. Every new counter joins the bounds tuple in `DeadlineDetail.__post_init__`.
- Capture/wait timeouts and `poll_interval` must be positive finite seconds, never bool. Required observation policies bound launches; cleanup may outlast the deadline and stops only local CLI processes, not daemon-side work.

## Wire And Identity

- Wire models are strict and keep only evaluated fields. Compose still requires `ComposeLabels.service`; other label types stay strict and unrelated labels drop. Standalone projections require no Compose labels. `Healthcheck` shrinks to presence (a nonempty `Test` whose first item isn't `NONE`); container environment and command drop.
- Service, task, and Compose container inspection need exactly one result whose full ID equals the request, else `ParsingError`. A service name or abbreviated ID therefore fails capture as `observation`; standalone capture deliberately resolves references instead.
- Non-job generations fence `Version.Index` plus a SHA-256 over complete `Mode` and `TaskTemplate`. Jobs fence that mode/template fingerprint plus `JobStatus.JobIteration.Index`, not `Version.Index`. The only template normalization drops `Runtime == "container"`; compare non-job task specs through `canonical(..., template=True)`.
- `target_from_wire` supports Replicated, Global, ReplicatedJob, and GlobalJob, failing closed otherwise. Replicated must be exactly `{Replicas: int >= 0}`. ReplicatedJob permits only positive integer `MaxConcurrent` (default 1) and `TotalCompletions` (default MaxConcurrent); GlobalJob must be `{}`. Jobs require `JobStatus`, else `malformed`.
- A missing image is `malformed`; non-container runtimes are `unsupported`. Node-membership requirements apply to both service and job variants.

## Swarm Evaluation

- `check_service` checks generation first (`superseded`). For non-jobs, paused, rollback, or failed update states are `update_failed`; unknown update states are `malformed`. Only desired-`running` tasks count; unknown task states raise `ParsingError`.
- Non-job Replicated needs every slot `1..replicas` running on the captured template. Non-job Global needs running matching tasks on exactly the approved nodes; duplicate, unassigned, or outside nodes block.
- Jobs consider only tasks with the captured iteration: `complete` tasks fill distinct slots `0..total_completions-1` for ReplicatedJob or approved nodes for GlobalJob. Matching-iteration `failed`/`rejected` tasks with restart condition `none` return cause-free `completion_failed`. Job update state is ignored.
- Once every target looks ready, `wait` re-inspects each service with the appropriate generation fence. Non-job update checks still apply, and `updating` clears readiness; jobs ignore update state here too. Rechecks, then `check_observation`, precede publishing `last_complete`.

## Standalone Capture And Membership

- `capture_containers` accepts a nonempty tuple of distinct nonempty names or ID prefixes. Resolution requires one matching result with a full lowercase 64-hex ID; different references resolving to the same ID yield cause-free `malformed`.
- `ContainerTarget` retains only resolved IDs, completion IDs, restart budget, and originating config. Each cycle inspects those fixed IDs, never re-resolves names: a missing container or changed returned ID is `superseded`. A missing container during capture remains `observation` with its cause.
- Both container captures validate selection, completion, restart budget, and dry-run configuration before launch. `completion=()` (standalone) and `completion_services=()` (Compose) must be distinct tuples drawn from the selected references/names. Dry-run capture raises `ValueError`; checks return `unsupported`.

## Compose Evaluation

- `capture_compose` requires a nonempty tuple of distinct nonempty service names. Capture lists with `compose ps --all --quiet`, so stopped containers count, and skips containers labelled `com.docker.compose.oneoff=True`, at capture and every cycle. No containers or mixed hashes is `unsupported`; a missing hash or a container outside the selection is `malformed`.
- Capture fixes completion designations, container membership, and hashes. Hashes don't cover replica count; the fixed IDs do.
- `check_compose` re-lists each cycle. A changed ID set, service label, or config hash, or a vanished container, is `superseded`, including for completion services.

## Shared Container Evaluation

- `evaluate_groups` counts Compose services as groups and standalone containers as singleton groups. Long-running members need `running`, not restarting or paused; configured healthchecks need Health, and any reported Health must be `healthy`. Unmarked exited-0 one-shots still block.
- Every captured completion container must be `exited`, exit code 0, and not restarting; health is not its readiness condition. A non-restarting nonzero exit returns cause-free `Failure("completion_failed")`; created, running or restarting containers remain pending.
- Required `State.ExitCode` is a strict integer: missing or malformed values yield cause-free `malformed`. Do not default a missing code to success.
- `unfinished_completion_containers` counts groups with any unfinished completion member, not individual containers in a Compose service; it stays bounded by `captured_services`.
- `max_restarts` (`None` or nonnegative int, never bool) is validated before launch and frozen on both target types, unset by default. Wire `restart_count` stays an unvalidated `JsonValue`, so disabled budgets ignore bad data. Enabled, capture and every cycle require integer `RestartCount >= 0` on each long-running member, else cause-free `malformed`; completion members ignore it. `restart_budget_exceeded_containers` counts affected groups, is live (not monotonic, no new failure reason), and blocks success.
- `wait_compose` and `wait_containers` delegate to `wait_groups`: after the first all-clear, run a second complete pass and require that pass clear too. Check observation before publishing `last_complete`; interruption retains only the previous complete cycle. `_compose._pause` uses `SLEEP_SLICE` (0.05 s), checks observation each slice, and caps the pause at the deadline.
- Compose and standalone paths turn `ValidationError`/`ParsingError` into cause-free `malformed`; `completion_failed` is cause-free too. Other observation errors keep their cause. Swarm parsing failures stay `observation` with cause; preserve that split.

## Tests

`tests/pywhales/test_convergence.py` drives a fake Python CLI (`fake_cli`, backed by `data.json` and a `calls` log) and needs no daemon. `test_convergence_types.py` holds `assert_type` checks for `ty`.

- Tests swap `time` in `_wait`, `_compose`, and `pywhales._observation`, patch `_wait.check_service`, and spy on `_wire.run`. Keep those module-level call sites or update the tests.
- Hung-child tests assert reaping, so cancellation and deadline paths must still kill the local CLI.
- Coverage includes standalone name/prefix capture, fixed membership, completion/restart rules, Swarm jobs, strict malformed output, count bounds, and the second all-clear pass.

```bash
uv run --locked pytest tests/pywhales/test_convergence.py -q
```

The fake CLI needs no daemon but starts real local child processes.
