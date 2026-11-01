# Convergence Contracts

Read-only observation of a captured Swarm service generation, a fixed stack, or selected Compose services. Nothing here creates, updates, or rolls back daemon state. Observation scopes, launch limits, and local process cleanup are in [the shared runtime guide](../AGENTS.md).

## Local Navigation

- `__init__.py` re-exports the public names. `_wait.py` holds `capture_service`, `capture_compose`, `wait`, and the Swarm polling loop.
- `_types.py`: frozen targets and outcomes. `_wire.py`: strict Pydantic wire models and exact-identity inspect helpers.
- `_service.py`: `check_service` and `ServiceObservation` for Swarm. `_compose.py`: Compose capture, per-cycle `check_compose`, and `wait_compose`.

## Types And Outcomes

- Dataclasses are frozen and slotted. `client_config` is `repr=False, compare=False` on `ServiceTarget` and `ComposeTarget`, as is `Failure.cause`; `template` and `approved_node_ids` leave `repr` but stay in equality. Preserve that distinction between client settings and captured generation data.
- `Deadline` accepts only a concrete `DeadlineDetail` (`type(...) is`), so a subclass can't smuggle a payload. The detail holds per-reason affected-service counts, each bounded by `captured_services`, with no identities, tasks, containers, settings, or causes.
- Detail is the latest complete cycle, never a partial one or a fresh inspection at expiry. It is absent when capture expired or no cycle finished.
- Swarm readiness is `ServiceObservation.ready`; Compose success is `_settled`, an all-zero `DeadlineDetail`. A new Swarm blocker needs a flag plus `summarize`. A new counter blocks Compose by default once `check_compose` fills it. Any new counter also joins the bounds tuple in `DeadlineDetail.__post_init__`.

## Wire And Identity

- Wire models are strict and keep only evaluated fields. `ComposeLabels` drops other labels, `Healthcheck` shrinks to presence (a non-empty `Test` whose first item isn't `NONE`), and container environment and command are dropped.
- Inspect helpers need exactly one result whose full ID equals the request, else `ParsingError`. A service name or abbreviated ID therefore fails capture as `observation`.
- A generation is `Version.Index` plus a SHA-256 over the complete `Mode` and `TaskTemplate`. The only normalization drops `Runtime == "container"` from the template. Ignore nothing else, and compare task specs through `canonical(..., template=True)`.
- `target_from_wire` fails closed. Replicated must be exactly `{Replicas: int >= 0}` and takes no approved nodes; Global requires them. Other modes or runtimes are `unsupported`, and a missing image is `malformed`.

## Swarm Evaluation

- `check_service` checks the generation first (`superseded`), then update state: paused, rollback, or failed states are `update_failed` and unknown states `malformed`. Only desired-`running` tasks count, and an unknown task state is a `ParsingError`.
- Replicated needs every slot `1..replicas` running on the captured template. Global needs running matching tasks on exactly the approved nodes; duplicate, unassigned, or outside nodes block.
- Once every target looks ready, `wait` re-inspects each service with the same generation and update checks, and an `updating` state there clears readiness. That recheck, then `check_observation`, must precede publishing `last_complete`.

## Compose Evaluation

- Capture lists with `compose ps --all --quiet`, so stopped containers count, and skips containers labelled `com.docker.compose.oneoff=True`, in capture and every cycle. A service with no containers or mixed hashes is `unsupported`; a missing hash or a container outside the selection is `malformed`. A dry-run client raises `ValueError` at capture and is `unsupported` in `check_compose`.
- `completion_services=()` is a tuple of distinct names, a subset of the nonempty distinct selected `services` tuple. Validate before launch; capture fixes the designation with the container membership and hashes.
- `check_compose` re-lists each cycle. A changed ID set, service label, or config hash, or a vanished container, is `superseded`, including for completion services.
- Long-running services need `running`, not restarting or paused, and `healthy` when a healthcheck is configured; missing Health blocks. Unmarked exited-0 one-shots still block.
- Every captured completion container must be `exited`, exit code 0, and not restarting; health is not its readiness condition. A non-restarting nonzero exit returns cause-free `Failure("completion_failed")`; created, running or restarting containers remain pending.
- Required `State.ExitCode` is a strict integer: missing or malformed values yield cause-free `malformed`. Do not default a missing code to success.
- `unfinished_completion_containers` counts affected **services**, once per service with any unfinished completion container. Include it in `DeadlineDetail.__post_init__`'s bounds tuple; it cannot exceed `captured_services`.
- `max_restarts` (`None` or nonnegative int, never bool) is validated before launch, frozen on `ComposeTarget`, and unset by default. `ContainerWire.restart_count` stays an unvalidated `JsonValue`, so a disabled budget ignores bad data as before. Enabled, capture and every cycle require an integer `RestartCount >= 0` on each long-running member (else cause-free `malformed`); completion services ignore it. `restart_budget_exceeded_containers` counts services with any container above budget, is live (no monotonic assumption, no new failure reason) and blocks success like any counter.
- `wait_compose` re-runs the check after the first all-clear and succeeds only if that final pass is clear too. `_pause` sleeps in `SLEEP_SLICE` (0.05 s) steps and checks the observation each time.
- Compose turns `ValidationError` and `ParsingError` into `malformed` with no cause. The Swarm path keeps `observation` with its cause. Keep that split.

## Tests

`tests/pywhales/test_convergence.py` drives a fake Python CLI (`fake_cli`, backed by `data.json` and a `calls` log) and needs no daemon. `test_convergence_types.py` holds `assert_type` checks for `ty`.

- Tests swap `time` in `_wait`, `_compose`, and `pywhales._observation`, patch `_wait.check_service`, and spy on `_wire.run`. Keep those module-level call sites or update the tests.
- Hung-child tests assert reaping, so cancellation and deadline paths must still kill the local CLI.
- Completion tests include `test_compose_mixed_selection_succeeds_when_completion_container_exited_zero`, `test_compose_completion_nonzero_exit_fails_immediately_without_payload`, and `test_compose_unfinished_counter_is_per_service_and_joins_other_blockers`.
- `test_compose_completion_without_a_usable_exit_code_is_malformed` and `test_compose_completion_success_needs_the_final_pass_to_finish_too` guard strict wire validation and the second recheck.

```bash
uv run --locked pytest tests/pywhales/test_convergence.py -q -k compose
```

The full file also covers the existing Swarm paths; the Compose selection still starts real local CLI children.
