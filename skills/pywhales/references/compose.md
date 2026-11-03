# Compose

## Scope Files and Project

Set Compose globals on a dedicated `DockerClient`, not on `compose.up()`.
`compose_files` accepts an ordered sequence of files. Use
`compose_env_files=[".env", ".env.local"]` for ordered environment files and
`compose_profiles=["test"]` when those inputs exist and the task needs them.
Don't change the user's active Docker context to select a Compose target.

For the disposable example below, put this in `compose.yaml` in the working
directory. For an existing project, use its actual files and service names.

```yaml
services:
  app:
    image: alpine:3.22
    command: ["sleep", "300"]
```

```python
from uuid import uuid4

from pywhales import DockerClient

client = DockerClient(
    compose_files=["compose.yaml"],
    compose_project_name=f"pywhales-demo-{uuid4().hex[:8]}",
    client_type="docker",
)
try:
    client.compose.up(["app"], detach=True, wait=True, wait_timeout=60)
    print(client.compose.execute("app", ["echo", "ready"], tty=False))
    print(client.compose.logs(["app"], tail="20"))
finally:
    client.compose.down()
```

This creates an isolated project and tears down its containers and networks.
`down()` keeps volumes by default. Use this cleanup only for a project you own
and are authorized to remove, not a shared stack. `volumes=True` also removes
declared named volumes and attached anonymous volumes; request it only when
deleting that data is part of the task. `remove_images` is another explicit
deletion choice, not a default cleanup setting.

## Output and Service Selection

| Operation | Result |
| --- | --- |
| `client.compose.build(...)` | `None`; normal output is displayed |
| `client.compose.up(...)` | `None`; it doesn't return containers or captured logs |
| `build/create/down/pull/start/stop/up(..., stream_logs=True)` | Typed, owned `ProcessStream` yielding `(source, bytes)`, or `None` for an explicit empty service list |
| `client.compose.ps()` | `list[Container]` for the selected project |
| `client.compose.logs(...)` | `str`; `stream=True` returns a typed `ProcessStream` yielding `(source, bytes)` |
| `client.compose.execute(service, command, tty=False)` | `str`; detached or TTY execution returns `None` |
| `client.compose.run(service, command, tty=False)` | `str`; `detach=True` returns `Container`, `stream=True` returns a typed `ProcessStream` yielding `(source, bytes)` |

`source` is `"stdout"` or `"stderr"`, never `"stdin"`. These streams own local
CLI processes and support context-manager cleanup; drain them to observe
failures. Closing them doesn't tear down the project.

```python
with client.compose.logs(["app"], tail="20", stream=True) as logs:
    for source, chunk in logs:
        print(source, chunk.decode(errors="replace"), end="")
```

Guard a possible `None` before using optional streams in a `with` block. The
typed return annotations don't change existing empty-selection behavior.

For `build` and `up`, `services=None` means all services while `services=[]`
means no work. `pull` has the same selection rule; `ps` instead treats both
`None` and `[]` as all services. For `logs`, omit the argument or pass `[]`/`()`,
not `None`: its signature takes a service string or sequence. Don't normalize
these differences.
`up` and `run` enable dependencies by default; `build`'s separate
`with_dependencies` option defaults to `False`.
`quiet=True` is incompatible with `stream_logs=True`.

## Distinguish Timeout Meanings

`down`, `restart`, and `stop` accept `timeout` as a Compose shutdown grace
period forwarded to the CLI, not a local subprocess deadline. `down` accepts
integer seconds; `restart` and `stop` also accept a `timedelta`, truncated to
whole seconds. These launches can still raise `DockerTimeout` if the client's
separate `default_timeout` expires.

`build`, `execute`, `run`, `logs`, `pull`, `push`, `watch`, and `events`
instead accept a keyword-only `timeout` in positive finite seconds that replaces
the client's `default_timeout` for that one local CLI launch; `None` inherits it.
`execute` applies it to detached, TTY, and captured calls alike. Expiry kills and
reaps the local Compose CLI and raises `DockerTimeout` with retained partial
output, not a replay of streamed stdout already delivered. It does not stop
services, undo built, pulled or pushed layers, or guarantee that daemon-side or
registry work stops. `build`, `pull`,
`push`, and `watch` validate it before the empty-selection no-op: an invalid
value raises `ValueError` even for `services=[]`, while a valid one with `[]`
returns `None` without a launch. A returned stream's deadline keeps advancing
while nothing consumes it, so drain it or close it on early exit.

Don't confuse the two: `compose.up(wait_timeout=60)` is a third, separate
concept, the Compose-side `--wait-timeout` bound on service readiness, not a
local CLI deadline either.

## One-Off Commands and TTY Defaults

Both `compose.run` and `compose.execute` default to `tty=True`. Set `tty=False`
for captured output and unattended jobs. `compose.run` also rejects TTY plus
streaming, TTY plus detach, and detach plus streaming.

For job stderr that may look like a Docker error, opt into
`DockerClient(strict_workload_errors=True)`. Non-detached `compose.run`
(captured, TTY, or streamed) and `compose.execute` (non-streamed, captured or TTY)
then raise exact `DockerException` on nonzero exit. Detached calls and other
Compose commands retain specialization; this doesn't add a stream option to
`execute`. See [Workload failures](client-and-errors.md#separate-workload-failures-from-docker-errors)
for the default, preserved raw attributes, and unaffected timeout/limit errors.

A detached `compose.run(..., tty=False, detach=True)` handle retains the complete
captured container ID and originating client configuration. Keep the full ID
when reloading or passing it to other container APIs; don't truncate it.

With the client and disposable Compose file above, a one-off job is:

```python
output = client.compose.run(
    "app", ["echo", "job complete"], tty=False, remove=True, dependencies=False
)
print(output)
```

`remove=True` removes that job container after exit; it doesn't clean up any
separately started service containers. Use `execute` for a command in an
already-running service. Collect logs separately from `up`; `follow=True` is
potentially unbounded, so use `stream=True` with a deliberate stop boundary.

Both methods accept `envs={"GREETING": "hello", "EMPTY": ""}` for container
values. Eligible values travel through the per-launch CLI environment and
`--env KEY`, so they can also affect `${NAME}` interpolation elsewhere in the
Compose configuration. Reserved or colliding names stay literal `KEY=VALUE` unless strict mode rejects
them; see [Container environment values](containers-and-output.md#container-environment-values).
Client `environment=...` is separate CLI configuration, not a replacement for
`envs`, and no temporary env files are created for this forwarding.

## Build And Run Environment

`build(build_args={...})` shares the same transport as `run` and `execute`
`envs`. An eligible build arg is passed as name-only `--build-arg KEY`, with its
value in the Compose CLI's child environment. Compose reads that environment for
`${KEY}` interpolation, so the value can also change interpolated settings and
name-only `environment:` entries of services, not just the build. Don't reuse a
build arg name for an unrelated variable the Compose file interpolates.

Reserved or colliding names stay literal `KEY=VALUE` arguments by default. With
`DockerClient(strict_env_transport=True)` they raise a value-free `ValueError`
instead; see [Strict environment transport](client-and-errors.md#strict-environment-transport).
Every `build` branch (blocking and `stream_logs=True`) and every `execute` branch
(detached, TTY, captured) forwards the same child environment.
The client's [environment inheritance setting](client-and-errors.md#isolate-the-cli-environment)
also applies to these launches and their collision checks.

`build(services=[])` is a validated no-op: it checks `timeout` and the
`quiet`/`stream_logs` conflict, then returns `None` without a launch. It never
evaluates `build_args`, so strict mode raises nothing for an empty selection.

## Plugin Features and Previews

`wait=True` waits for running/healthy services and implies detached mode in
Compose. Newer options, such as build `check`/`print` and up `watch` or
`exit_code_from`, require a supporting plugin. PyWhales forwards flags without
version probing or automatic downgrade. Blocking `compose.build(print=True)`
still returns `None`; stream it if the output must be consumed.

`compose.watch(...)` itself returns an owned `(source, bytes)` stream; it
doesn't take `stream_logs=True`. It defaults to `prune=False` (explicitly
forwarded as `--prune=false`, and `--prune=true` only when requested), and an
explicit empty service list returns `None` without invoking the CLI. A watch
can run until stopped: pass a `timeout` or a deliberate stop boundary, and use
the stream as a context manager. Closing it reaps the local CLI without
removing running services.

`wait=True` is a Compose CLI option applied during `up`. To check afterward
that a chosen set of services settled, use the read-only
`capture_compose()` and `wait()` from `pywhales.convergence`, documented in
[Observation budgets and convergence](observation-and-convergence.md). Capture
takes explicit, distinct service names for the client's configured project, and
accepts `completion_services=("migrate",)` for selected one-shot services expected
to exit 0. Every captured completion container must have exited 0 and not be
restarting; a non-restarting nonzero exit fails as `completion_failed`. Unmarked
services still require running/healthy containers; by default an unmarked
one-shot that exited 0 stays a blocker. Set `fail_on_exit=True` on
`capture_compose` so convergence `wait()` returns cause-free `Failure(reason="exited")` when a
long-running member exits without restarting, whether its code is zero or
nonzero. The choice is frozen on the target and applies to the confirming pass
too; designated completion failures take precedence, enabled restart counts
must remain valid, and over-budget members remain blockers. This option does
not change `compose.up(wait=True)`; see the convergence reference for the
capture/wait workflow and validation.

`capture_compose(..., fail_on_unhealthy=True, policy=..., timeout=...)` separately
opts into cause-free `Failure(reason="unhealthy")` for long-running members that
are running, unpaused, not restarting, within any restart budget, and report
exactly `unhealthy`. The flag must be an exact bool, defaults to `False`, and
is frozen on the target. Completion ignores health. Missing health blocks only
configured healthchecks; any reported non-healthy status blocks readiness even
without a configured check. Failed completion, restart-count validation, and
eligible `exited` failures take precedence. See the convergence reference for
confirming-pass behavior; this doesn't change `compose.up(wait=True)`.

`capture_compose(..., max_restarts=None, policy=..., timeout=...)` leaves restart
counts unchecked by default. Set a nonnegative integer (never a boolean) to fix
a budget for each long-running container's current Docker `RestartCount`, not
the number of restarts since capture. A healthy container above that budget
still blocks success; a later within-budget reading clears the blocker.
Completion services ignore this budget. With it enabled, a missing, negative,
or non-integer count on a long-running container is `malformed`.

The budget is rechecked in the final all-clear pass. On deadline,
`Deadline.detail.restart_budget_exceeded_containers`, when detail is present,
counts affected services once each despite its name, not individual containers.
It describes the latest complete cycle, not fresh state at expiry. See
[Observation budgets and convergence](observation-and-convergence.md) for the
capture/wait workflow and typed outcomes.

Preview mode is the client global `compose_dry_run=True`, not an `up()` keyword.
Use a separate preview client with the same files/project settings. It still
invokes the Compose plugin, and not every command supports dry run.
`compose.ps()` and detached `compose.run()` reject this mode before execution
because preview output cannot supply real container objects. Don't treat it
as an offline validator or disable the preview silently to make a call work.
