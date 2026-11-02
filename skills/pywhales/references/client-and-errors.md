# Client, Setup, and Errors

## Prerequisites

Use Python >=3.14.10. In a uv-managed application, add the dependency with
`uv add pywhales`. The package doesn't supply the Docker CLI: Docker must be on
PATH, and the selected engine must be reachable. Compose and Buildx commands
also need their respective Docker CLI plugins.

Check `docker version` for client/server connectivity. Check `docker compose
version` or `docker buildx version` only when that plugin is needed. A successful
Python import or client construction doesn't test any of these prerequisites;
CLI lookup is deferred until a command needs it.

## Choose a Client

```python
from pywhales import docker

version = docker.version()
if version.client is not None:
    print(version.client.version)
```

`docker` is a ready-made `DockerClient(client_type="docker")`. For a separate
target, construct a scoped client rather than changing the active CLI context:

```python
from pywhales import DockerClient

remote = DockerClient(host="ssh://user@host", client_type="docker")
staging = DockerClient(context="staging", client_type="docker")
```

Those are alternatives; select the host or context the user supplied. SSH
authentication and access to the remote engine must already work. Bind-mount
source paths refer to the daemon host, not automatically to the Python machine.

Keep Compose globals on its client: `compose_files`, `compose_profiles`,
`compose_project_name`, `compose_project_directory`, and `compose_env_files`.
Environment files are applied in order, with later files overriding earlier ones.

`environment={"KEY": "value"}` overlays the child's base environment for every
launch from that client. By default, `inherit_environment=True` copies the
process environment; `environment=None` adds no overlay. This does not affect
executable lookup or the parent Python process's own environment.

Keep this CLI configuration distinct from container `envs={...}`. Eligible
`envs` values use a per-launch child environment with `--env KEY`; CLI-control
names and environment collisions stay literal `KEY=VALUE` arguments by default.
Neither path is a secret vault. See
[Container environment values](containers-and-output.md#container-environment-values),
including the Compose interpolation caveat.

If passing an existing `client_config=...`, configure that object directly,
`inherit_environment`, `strict_env_transport`, and `strict_workload_errors` included: the constructor retains it unchanged and
ignores the other constructor options.

## Isolate The CLI Environment

```python
from pywhales import DockerClient

client = DockerClient(
    inherit_environment=False,
    environment={"DOCKER_CONTEXT": "ci"},
)
```

Select an existing authorized context in place of `ci`. With inheritance off,
the child retains only the available `PATH` and `HOME` entries from the parent,
plus `USERPROFILE` and `SYSTEMROOT` on Windows; Windows names match
case-insensitively. The explicit client overlay and per-launch overrides are
applied on top. Ambient `DOCKER_*`, `SSH_AUTH_SOCK`, tokens, and proxy variables
aren't inherited: pass any needed entries explicitly through `environment=`.
Executable lookup still uses the Python process's `PATH`, not the overlay.
Retained home paths can still select Docker configuration and credential helpers;
this flag controls environment inheritance, not all credential or filesystem access.

The setting covers captured calls, streams, image load/save, and inventory
capture. Each launch keeps its selected environment; staged inventory reuses its
frozen group snapshot. Environment-only container/build forwarding checks that
same effective environment: an ordinary name found only in excluded ambient
state no longer collides. Reserved CLI names still require literal fallback or
fail in strict mode. Keep the config stable when using endpoint-bound policy
objects; changing this compared setting invalidates an existing endpoint binding.

## Strict Environment Transport

`strict_env_transport` defaults to `False` on `DockerClient(...)` and on the
`ClientConfig` dataclass, which you import from `pywhales.client_config`; the
package root doesn't export it. Leaving it off keeps the literal `KEY=VALUE`
fallback described above.

```python
from pywhales import DockerClient

strict = DockerClient(strict_env_transport=True)
```

With `True`, container `envs` and Buildx or Compose `build_args` must all travel
as name-only `--env KEY` or `--build-arg KEY`, with the value in that launch's
child environment. An entry that would need the literal form raises a
`ValueError` whose fixed message names no entry and shows no value, and the whole
call fails. There's no per-call switch; the setting belongs to the client.

This narrows what reaches argv. It isn't redaction or a secret vault: child
environments, Compose interpolation, image history and provenance for build
args, returned data, raw exception attributes, and your own logging can still
carry values. Which entries count as literal fallbacks, and when the rejection
happens, depends on the call. See
[Container environment values](containers-and-output.md#container-environment-values),
[Build arguments](buildx.md#build-arguments), and
[Compose build and run environment](compose.md#build-and-run-environment).

## Default CLI Deadlines

`DockerClient(default_timeout=30)` applies a 30-second deadline to each local
CLI launch from that client. The default is `None`, which leaves launches
unbounded. Where a method accepts a per-call local CLI deadline as `timeout`,
it's keyword-only: `None` inherits the client default and a positive finite explicit
value replaces it for the selected launch(es) only. Booleans, zero, negatives,
and nonfinite values are invalid and raise `ValueError` before any launch;
`None` doesn't disable a configured default. This isn't a whole-operation budget:
preflight inspection, implicit image pulls, and postflight inspection are separate
launches governed by their own settings. Inherited defaults are snapshotted at
each launch, including lazy archive generators and queued transfers.

Docker option timeouts are different: Compose `down`, `restart`, and `stop`
use `timeout` for shutdown grace periods, not local CLI deadlines. See
[Distinguish timeout meanings](compose.md#distinguish-timeout-meanings).

Expiry kills and reaps the local CLI and raises `DockerTimeout`, a subclass of
`DockerException` with the same `docker_command`/`return_code`/`stdout`/`stderr`
attributes. This happens even while archive consumption is paused or a producer
is blocked; arbitrary Python iterator code itself cannot be interrupted, so the
timeout is only reported when the caller's code resumes. Retained partial output
may be available on the raw attributes; streamed stdout already delivered to the
consumer isn't reconstructed there. Expiry does not guarantee
daemon-side cancellation or rollback. See
[Containers, output, and lifecycle](containers-and-output.md) and
[Registry transfers and archives](registry-and-archives.md) for which methods
accept a per-call `timeout`. Container, image, network, volume and system prune
also accept it; see [Bound prune CLI launches](containers-and-output.md#bound-prune-cli-launches)
for result shapes, filters and destructive-timeout handling.

## Handle the Right Failure

```python
from pywhales import ClientNotFoundError, DockerException, docker

try:
    version = docker.version()
except ClientNotFoundError as error:
    raise SystemExit("Docker CLI is missing or isn't on PATH.") from error
except DockerException as error:
    print(f"Docker command failed with exit code {error.return_code}")
    raise
else:
    if version.client is not None:
        print(version.client.version)
```

`ClientNotFoundError` is separate from `DockerException`. The latter represents
a CLI failure (including local deadline expiry) and exposes
`docker_command: list[str]`, `return_code: int`,
and `stdout`/`stderr` as `str | None`. Output can be `None` when it wasn't
captured. More specific errors, such as `NoSuchContainer`, `NoSuchImage`, and
`DockerTimeout` (a local CLI deadline expiry, not a daemon-side timeout), are
imported from `pywhales.exceptions`.

Use the exit code and relevant, redacted output to distinguish daemon access,
missing resources, registry authentication, and unsupported plugin flags.
Don't retry a mutating command just because it failed. Invalid Python arguments
may raise `TypeError` or `ValueError` before a subprocess is started.

For registry login, `docker.login(server=..., username=..., password=...)`
passes the password through stdin, always requesting `--password-stdin`.
Omitting `password` (`None`) still requests `--password-stdin` but inherits
the caller's own stdin instead of writing a value; pass an explicit password
when running non-interactively. Obtain credentials from the application's
existing secret mechanism; don't embed them in generated examples or reports.

### Separate Workload Failures From Docker Errors

By default, forwarded workload stderr can select a specialized exception: a job
that prints `Error: No such container: db` can look like `NoSuchContainer`.
For user-submitted jobs or cleanup logic that relies on those types, opt in on
the client rather than trying to recognize workload stderr yourself:

```python
from pywhales import DockerClient

jobs = DockerClient(strict_workload_errors=True)
```

The default is `False` on both `DockerClient` and `ClientConfig`. With it enabled,
nonzero exits from attached container `run`, non-detached `execute`, attached
`start`, and non-detached Compose `run`/`execute` raise exactly `DockerException`,
not a stderr-selected subclass. This covers captured/TTY execution and supported
streams, including a failure observed before iteration or at exhaustion. Compose
`execute` still has no stream mode. The exception retains its `docker_command`,
`return_code`, `stdout`, and `stderr` attributes (output may be `None` when
uncaptured), with the same safe/raw rendering policy.

Detached/unattached launches, implicit image pulls, inspection preflights, and
other commands retain their existing specialization. A specialized preflight
error can therefore still come from an attached call. The setting does not
distinguish actual Docker errors within a marked launch: all its nonzero exits,
including code 125, stay generic. Don't infer resource absence from workload
stderr. `DockerTimeout`, observation cancellation/limit errors, and telemetry
outcomes are unchanged. The originating config is snapshotted per launch, so
later mutations don't change a stream already running. Check installed support
as described in [Usage validation](validation.md).

## Troubleshooting Without Credential Leaks

`PYWHALES_DEBUG=1` enables execution diagnostics, with command and environment
values withheld by default. A separate `DockerClient(raw_diagnostics=True)`
opts into detailed logs and errors, including the merged environment in debug
printing and inspect-response dumps on model failures. Use that opt-in only in
a trusted troubleshooting environment. Only the exact value `1` enables debug
printing. Raw exception attributes, returned
payloads, uncaptured terminal output, and caller logging remain outside this
policy.

Malformed responses from Buildx builder reload/list, Compose config/ls/events,
system disk-free/events/info, and imagetools manifest parsing raise
`ParsingError` from `pywhales.client_config`, not a `ValueError` subclass.
Safe mode suppresses the payload-bearing parsing chain; raw diagnostics chains
the original error. Event parsing keeps the launch-time diagnostics setting.
`version()` and container stats keep their existing exception behavior.

Scalar inspection of container, image, network, volume, config, secret, node,
service, task, plugin, and context handles requires exactly one JSON object in
the CLI's response array. Empty/multiple results, non-object/non-array shapes,
or invalid JSON raise `ParsingError`. A reload that returns a different stored
ID or name also raises `ParsingError` without replacing the cached snapshot or
its refresh timestamp. A current-context handle tracking `None` may follow a
changed current context. Initial name/prefix resolution is still allowed;
reloads use the resolved identity. Safe diagnostics dump no response and suppress
payload-bearing causes; raw diagnostics can expose original errors and dumps.
These checks don't replace command failures with parsing failures: classified
`DockerException` subclasses retain their raw command, status, and output fields.
Builder listings, manifest object responses, and batch inspection retain their
separate contracts; don't assume the scalar array rule for them.

If an argument is rejected, inspect that method in the installed package before
changing versions or guessing SDK-style names. New Compose flags are forwarded
to the plugin without a compatibility probe or automatic fallback.

For opt-in sanitized execution telemetry or application-owned concurrency
limits around specific calls, see
[Telemetry and admission](telemetry-and-admission.md). For bounding batch
inspection or waiting for Swarm node readiness, service/job/stack, Compose project, or standalone container convergence,
see [Observation budgets and convergence](observation-and-convergence.md).

Application policy APIs also require explicit imports from `pywhales.profiles`,
`pywhales.reconciliation`, or `pywhales.storage`, not the package root. They
don't automatically add create policy, reconciliation, or storage gates to
ordinary `DockerClient` calls.
