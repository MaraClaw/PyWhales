# Containers, Output, and Lifecycle

## Translate a Foreground Run

```python
from pywhales import docker

output = docker.run(
    "alpine:3.22",
    ["printenv", "GREETING"],
    envs={"GREETING": "hello"},
    remove=True,
)
print(output)
```

This captures stdout as `str`, inherits stderr, and removes the disposable
container when it exits. Commands must be sequences, not `"printenv GREETING"`.
Use string values in `envs`.

| CLI option | Python shape |
| --- | --- |
| `-e KEY=value` | `envs={"KEY": "value"}` |
| `-p 8080:80` | `publish=[(8080, 80)]`, host port first |
| `-p 127.0.0.1:8080:80` | `publish=[("127.0.0.1:8080", 80)]` |
| `-v source:/work:ro` | `volumes=[(source, "/work", "ro")]` |
| `--rm` | `remove=True` |
| `-d` | `detach=True` |

Use an absolute host path for a bind mount; `pathlib.Path` is accepted. For
example, with `source = Path.cwd().resolve()`, mount that directory read-only
and run `["ls", "/work"]`. Import `Path` from `pathlib`. A remote engine must
have the source path on its own host.

## Startup Health Interval And Approved Profiles

Container `create` and every `run` mode, including `docker.create`/`docker.run`
aliases, accept `health_start_interval=None`. A supplied value must be positive
whole seconds: an exact `int` (not a bool or float), or an integral-second
`datetime.timedelta`. Invalid values raise `ValueError` before image preflight
or CLI lookup. It forwards `--health-start-interval <seconds>s`; omission leaves
the image setting unchanged. This is the check interval during the startup
period, not `health_start_period`, `health_interval`, or a convergence timeout.
The operator must qualify the selected CLI and engine for Docker API 1.44+.
PyWhales doesn't probe versions, retry without the flag, or silently downgrade.

```python
from pywhales import docker

# Run only on an authorized, operator-qualified API 1.44+ target.
with docker.run(
    "alpine:3.22",
    ["sleep", "30"],
    detach=True,
    health_cmd="true",
    health_start_period=20,
    health_start_interval=2,
    stop_timeout=1,
) as container:
    print(container.id)
```

For application-approved creation, import `CreateRequest`, `SecurityProfile`,
`RuntimePolicy`, `plan_create`, and `create_from_plan` from `pywhales.profiles`,
not the package root. Direct calls don't apply profiles automatically.
`CreateRequest.health_start_interval` accepts only `None` or a positive exact
integer in seconds, not a timedelta. The profile's
`max_health_start_interval=None` means no approval, not an unlimited interval;
an explicit positive exact integer ceiling is required for a supplied request.
Above-ceiling requests fail with `PolicyReason.LIMIT`; missing approval uses
`NOT_APPROVED`. Invalid request or ceiling types use `INVALID`.

The selected `RuntimePolicy.supported_controls` must include the baseline
`CONTROLS` and, only when an interval is requested, `"health_start_interval"`.
Declare that extra control only after qualifying this runtime; don't mutate
`CONTROLS` or add it to every runtime by assumption. Missing runtime support is
`UNSUPPORTED`. Omitted intervals need no new runtime control or profile ceiling.

Given the application's existing `client`, `profile`, and `request`, and explicit
operator approval of the selected runtime and a 5-second ceiling:

```python
from dataclasses import replace

from pywhales.profiles import CONTROLS, RuntimePolicy, plan_create

qualified_runtime = RuntimePolicy(
    name=request.runtime,
    rootless=request.rootless,
    supported_controls=CONTROLS | {"health_start_interval"},
)
approved_profile = replace(
    profile, max_health_start_interval=5, runtimes=(qualified_runtime,)
)
requested = replace(request, health_start_interval=2)
plan = plan_create(approved_profile, requested, client)  # no Docker I/O
```

`plan_create` performs pure validation. `create_from_plan(client, plan)` rechecks
the plan and endpoint binding before preparation, pull, inspect, or creation,
then forwards the approved interval to `client.container.create`. It creates
but doesn't start the container; the application owns its later lifecycle.

## Container Environment Values

`envs={...}` sets values inside the container. In container `create`, `run`, and
`execute`, service `create`, and Compose `run` and `execute`, eligible ordinary
names use `--env KEY`: PyWhales puts the value in that launch's child environment
for the CLI to forward. Empty values remain empty strings. This doesn't change
the parent Python environment or use temporary env files.

By default these entries stay literal `KEY=VALUE` arguments, in their original
order, instead of being rejected:

- CLI-control names, matched case-insensitively: the prefixes `DOCKER_`,
  `COMPOSE_`, `BUILDX_`, `BUILDKIT_`, `XDG_`, `SSH_`, `GIT_`, `OTEL_`, `LD_`,
  `DYLD_`, `LC_`, `NODE_`, and `PYTHON`, plus names such as `PATH`, `HOME`,
  `TMPDIR`, `TERM`, `LANG`, `TZ`, and proxy variables.
- Invalid entries: an empty name, a name containing `=` or NUL, a value
  containing NUL, or a non-string key.
- Duplicate names after stringification, including names that differ only by
  case on Windows.
- Names colliding with the effective child environment or an active
  frozen environment snapshot, compared case-insensitively on Windows.

With `inherited_environment_names` supplied, or legacy `inherit_environment=False`
when names are unset, ordinary names present only in excluded ambient state
don't collide. Selected ambient names and explicit client-overlay entries still
do, and Windows compares names case-insensitively. The forwarding decision keeps
its frozen environment through launch even if config changes during argument
conversion. Reserved names remain reserved. See
[CLI environment isolation](client-and-errors.md#isolate-the-cli-environment).

`DockerClient(strict_env_transport=True)` turns every entry above into a
`ValueError` with a fixed message that discloses no name or value; one ineligible
entry fails the whole call. See
[Strict environment transport](client-and-errors.md#strict-environment-transport).
The same check covers container `create`/`run`/`execute`, service `create`,
Compose `run`/`execute`, and Buildx or Compose build args. For container `run`
and `create`, the `pull="missing"` or `"always"` image step runs before `envs`
are evaluated, so a strict rejection can come after an image pull.

This reduces eligible values in argv; it doesn't promise that secrets never
appear there, and a child environment isn't a secret vault. Use the application's
secret policy. `DockerClient(environment=...)` configures the CLI environment,
not container values; keep it distinct from `envs`. For Compose, forwarded
values also enter the Compose CLI's environment and can affect `${NAME}`
interpolation elsewhere in its configuration.

## GPUs, vGPU and CDI Devices

GPU support is a property of the selected daemon (its context, host, or SSH
endpoint), not of the machine running Python. Point a scoped client at the GPU
host and ask it:

```python
from pywhales import DockerClient

gpu_host = DockerClient(context="gpu-01")  # or host="ssh://ops@gpu-01.example"
support = gpu_host.system.gpu_support()  # one `docker system info` launch
print(support.gpus_available, support.gpu_devices)
```

`gpu_support()` returns a frozen `GpuSupport` from
`pywhales.components.system.gpu` (the package root doesn't export it):

| Attribute | Meaning |
| --- | --- |
| `os_type` | Daemon container OS; GPU requests need `"linux"` |
| `gpu_runtimes` | Registered `nvidia` runtime, meaning the NVIDIA Container Toolkit is installed |
| `cdi_spec_dirs` | CDI specification directories; empty when CDI is off or unreported |
| `cdi_devices` | Discovered CDI device names, or `None` on daemons before Docker 28: unknown, not empty |
| `gpu_devices` | Discovered devices of a `--gpus` kind: `nvidia.com/gpu` or `amd.com/gpu` |
| `whole_gpus`, `gpu_uuids`, `mig_devices` | `gpu_devices` grouped by name: bare indexes (`nvidia.com/gpu=0`), `GPU-<uuid>` names, and MIG partitions (`0:1` or `MIG-<uuid>`) |
| `gpu_count` | Number of `whole_gpus`, or `None` when `cdi_devices` is `None`; MIG partitions and aliases such as `all` aren't counted |
| `gpus_available` | Linux daemon with a GPU runtime or GPU CDI device |

An NVIDIA vGPU is assigned to the VM by the hypervisor, so inside the guest
Docker sees an ordinary NVIDIA GPU. vGPU guests and MIG slices appear as
`nvidia.com/gpu=...` CDI names, for example `nvidia.com/gpu=0`,
`nvidia.com/gpu=0:1` (MIG slice) or `nvidia.com/gpu=GPU-<uuid>`. Those names come
from the host's CDI specification (`nvidia-ctk cdi list` on that host).

Select GPUs in one of two ways:

| CLI option | Python shape |
| --- | --- |
| `--gpus all` | `gpus="all"` |
| `--gpus 1` | `gpus=1` |
| `--gpus device=0` or `device=GPU-<uuid>` | `gpus="device=0"` |
| `--gpus '"device=0,2"'` | `gpus='"device=0,2"'`; keep the inner quotes |
| `--device nvidia.com/gpu=0:1` | `devices=["nvidia.com/gpu=0:1"]` |

Keep the inner double quotes for a device list: there's no shell to strip them,
and Docker parses the value as CSV, so `device=0,2` alone splits at the comma.
CDI names in `devices` need Docker 28.2+ with CDI enabled (the default) and a
CDI spec on the host. Prefer them for a specific vGPU or MIG slice: on Docker
29.2+, `gpus="all"` may not expose MIG devices. vGPU profile size and MIG
partitioning are set on the host or hypervisor, not through Docker.

### Request a Number of vGPUs

Each vGPU the hypervisor assigned to the VM is one GPU to Docker. Request N of
them in one of three ways:

```python
gpu_host.run(image, cmd, gpus=2)  # --gpus 2: any two GPUs
gpu_host.run(image, cmd, gpus='"device=0,1"')  # exactly GPUs 0 and 1
gpu_host.run(
    image, cmd, devices=["nvidia.com/gpu=0", "nvidia.com/gpu=1"]
)  # one CDI device per name
```

`gpus` accepts an `int` and forwards it unchanged as `--gpus N`. Docker hands out
whole GPUs, so a MIG slice never counts toward `N`; request slices by CDI name.
Two limits come from the hypervisor, not Docker or PyWhales: a container can't
get more vGPUs than the VM was assigned, and there's no fractional request. Each
vGPU's size (for example 8 GB) is fixed by its vGPU profile when the VM is
created.

`require_gpu=True` checks a count against the daemon's discovered names. Docker's
CDI path turns `--gpus N` into the literal names `nvidia.com/gpu=0` through
`nvidia.com/gpu=<N-1>` (indexes, not any N GPUs), so `gpus=4` on a VM with one
vGPU raises `gpu_device_missing` before the image pull, with
`error.devices == ("nvidia.com/gpu=1",)`. A selection such as
`gpus='"device=0,2"'` or `device=GPU-<uuid>` resolves to `nvidia.com/gpu=<id>` the
same way. Count the GPUs a host offers with `support.gpu_count`.

Add `require_gpu=True` to container `create()` or any `run()` mode, including
the `docker.create`/`docker.run` aliases, to check the target first:

```python
from pywhales.exceptions import GpuSupportError

try:
    container = gpu_host.run(
        "nvidia/cuda:13.0.2-base-ubuntu24.04",
        ["sleep", "infinity"],
        detach=True,
        devices=["nvidia.com/gpu=0:1"],
        require_gpu=True,
    )
except GpuSupportError as error:
    print(error.reason, error.devices)
```

The preflight runs after the `timeout`, `health_start_interval` and command
checks and before the image pull, so a host without GPUs fails before a large
CUDA download. A `detach=True, stream=True` conflict is reported only later.
It is one extra `docker system info` launch, outside the call's `timeout`; the client's
`default_timeout` still applies to it. `devices` is read once, so an iterator
is safe. It checks `gpus` and every qualified CDI name in `devices`; host paths
such as `/dev/fuse` aren't checked. With neither a `gpus` value nor a CDI name,
`require_gpu=True` raises `ValueError` without launching anything. Default calls
never probe.

The count and selection check is conservative. It refuses a resolved name only
when the daemon reports at least one name of the same kind and naming family
(whole index or `GPU-` UUID), so a UUID request on a daemon that reports only
indexes passes, and so does an index request on a MIG-only daemon. MIG names
(`0:1`, `MIG-...`) in a count or selection are never verified, because MIG layouts
change at runtime; explicit CDI names in `devices` still need an exact match
(`cdi_device_missing`). `gpus="all"` isn't count-checked. A runtime-only host (an
`nvidia` runtime and no reported CDI devices) passes any count, because there are
no names to compare. Docker's NVIDIA driver tries CDI first and falls back to the
legacy runtime hook on any CDI failure, so a `gpu_device_missing` refusal assumes
the host's CDI spec is current; regenerate it with `nvidia-ctk cdi generate` after
changing GPUs. `error.devices` lists missing indexes only up to the first one past
the highest reported index, so a huge count stays bounded.

| `GpuSupportError.reason` | Meaning |
| --- | --- |
| `not_linux` | The daemon doesn't run Linux containers |
| `no_gpu_driver` | `gpus` was requested, but there's no `nvidia` runtime and no GPU CDI device; a count isn't compared |
| `cdi_devices_unreported` | The daemon predates the discovered-device list, so CDI names can't be verified |
| `cdi_device_missing` | Requested CDI names aren't discovered; `error.devices` lists them in request order |
| `gpu_device_missing` | A `gpus` count or device selection resolves to GPU names the daemon doesn't list, never MIG names; `error.devices` lists them, deduplicated, in first-seen order |
| `no_node_capacity` | Swarm `service.create` only: no ready, active node advertises enough of a requested generic resource; `error.devices` lists the unsatisfied `kind=value` requests |

Container and Compose checks refuse in table order, so the first applicable reason
wins. The message names only the reason. `GpuSupportError` is not a `DockerException`.

The evidence is incomplete in one direction: a host that serves `--gpus` only
through the toolkit hook, with no registered `nvidia` runtime and no CDI spec,
reports `no_gpu_driver` even though a real run would work. Leave `require_gpu`
off there. The legacy route `runtime="nvidia", envs={"NVIDIA_VISIBLE_DEVICES":
"0"}` is not a GPU request, so `require_gpu` can't check it either.

Without the preflight, the daemon refuses unsatisfiable requests itself.
`create()` accepts `--gpus` and unknown CDI names; the refusal comes only when
the container starts. A detached or streamed `run()` captures stderr and raises
`GpuUnavailable`, a `DockerException` subclass. A foreground `run()` forwards
stderr to the terminal and raises a plain `DockerException`. With
`strict_workload_errors=True`, streamed runs also raise exact `DockerException`;
only detached runs keep `GpuUnavailable`.

### Swarm Services and Compose

Swarm `service.create()` and Compose `up()`/`run()` take `require_gpu=True` too;
Buildx has no GPU preflight. Each check targets the client's own daemon or Swarm,
is opt-in, and leaves Docker the final decision. Default calls never probe.

```python
swarm = DockerClient(host="ssh://ops@manager-01.example")
swarm.service.create(
    image, cmd, name="gpu-job", generic_resources=["gpu=2"], require_gpu=True
)

project = DockerClient(host="ssh://ops@gpu-01.example", compose_files=["compose.yaml"])
project.compose.up(["trainer"], detach=True, require_gpu=True)
project.compose.run("trainer", ["nvidia-smi", "-L"], tty=False, require_gpu=True)
```

For a service, `generic_resources` needs a nonzero `kind=int` request, or the call
raises `ValueError` without a launch. After the `timeout` and option validation,
it runs `node ls` plus one batched `node inspect` on the selected manager, outside
`timeout`, then the single `service create` launch. One ready, active node must
advertise every request (discrete values plus the count of named resources per
kind). Requests aren't summed across nodes, though duplicate kinds in
`generic_resources` are summed (the Docker CLI rejects them) and an amount may
carry a leading `+`. Drained, paused or not-ready nodes are ignored. Placement constraints and capacity that running tasks claim are
ignored, so a request can pass and still stay pending, but one that a ready,
active node advertises is never refused. A refusal has reason `no_node_capacity`;
on success the argv is unchanged.

For Compose, the check adds two launches, `compose config` (normalized JSON) and
`docker system info`, with the client's default deadline, outside `run()`'s
`timeout` (`up()` has none). `config` names the selected services, so profile-gated
services that `up`/`run` enables are rendered, and an unknown service fails it with
`DockerException`. It reads `gpus`, `deploy.resources.reservations.devices` entries
that name an NVIDIA or AMD GPU capability (`gpu`, `nvidia`, `compute`, `utility`,
`amd` and the like) or use the `nvidia`/`cdi` driver, and `devices` mappings whose
`source` and `target` are the same CDI name; a reservation with no count and no
device IDs means all GPUs, which isn't count-checked. It covers the selected
services (all for `None`) plus their `depends_on` closure unless
`dependencies=False`. `up()` skips a service whose instance count is 0 (`scales`,
else `scale`, else `deploy.replicas`) but still checks what it depends on; `run()`
always checks its own service and skips only dependencies scaled to 0. With no GPU request it raises `ValueError` after `config` and before
`info`, and `up(services=[])` stays a no-op without a probe. It runs after the
call's arguments are validated and encoded, just before the launch, so a bad
argument raises before either probe.

`support.check(gpus=..., devices=...)` runs the container check on a `GpuSupport`
you hold. `GpuRequest`, `compose_gpu_requests`, `generic_resource_requests` and
`check_generic_resources` also live in `pywhales.components.system.gpu`; the package
root exports none of them.

## Choose the Result Type

| Call mode | Result |
| --- | --- |
| `docker.run(...)` | `str` containing captured stdout |
| `docker.run(..., detach=True)` | `Container` |
| `docker.run(..., stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.execute(container, [...])` | `str` containing stdout |
| `docker.execute(container, [...], detach=True)` | `None` |
| `docker.execute(container, [...], stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.logs(container)` | `str` assembled from stdout/stderr log chunks |
| `docker.logs(container, stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.start(container)` | `None` |
| `docker.start(container, attach=True)` | `str` containing captured stdout |
| `docker.start(container, attach=True, stream=True)` | Owned `ProcessStream` yielding `(source, bytes)` |
| `docker.ps(all=True)` | `list[Container]`, including stopped containers |
| `docker.wait(container)` | `int` exit code |
| `docker.wait([container, ...])` | `list[int]` in Docker's output order; `[]` for an empty iterable, with no launch |

Streaming `container.run`, `container.prune(stream_logs=True)`, and
`service.logs(stream=True)` are also typed as closeable `ProcessStream` returns.
Container prune is destructive and suppresses confirmation; stream cleanup
doesn't undo deletions, so require explicit authorization before calling it.

For stream tuples, `source` is `"stdout"` or `"stderr"`. Don't decode the tuple;
decode its byte payload. Don't combine `detach=True` with streaming. For
`execute`, `tty=True` is also incompatible with streaming. Leave TTY allocation
off for captured automation; use `interactive=True, tty=True` for an actual
interactive terminal session. `start(..., stream=True)` requires `attach=True`,
and attaching more than one container raises `ValueError`; an empty iterable
returns `None` without a launch. `docker.start`, `docker.container.start`, and
`Container.start()` share these rules and their return shapes.

For workloads whose stderr may resemble Docker errors, configure
`DockerClient(strict_workload_errors=True)`. Attached `run`, non-detached
`execute`, and `start(attach=True)` then raise exact `DockerException` on nonzero
exits, in captured/TTY and supported streamed modes, including handle and root
aliases. Detached/unattached calls and image/inspect preflights keep specialized
errors. See [Workload failures](client-and-errors.md#separate-workload-failures-from-docker-errors)
for defaults, preserved error attributes, and timeout/cancellation behavior.

`run`, `execute`, `logs`, `start`, and `wait` each accept a keyword-only `timeout` in
positive finite seconds, replacing the client's `default_timeout` for that one
launch; omitting it (`None`) inherits the client default, which is itself
`None` (unbounded) unless set on `DockerClient(...)`. Expiry kills and reaps the
local CLI and raises `DockerTimeout` with retained partial output, not a replay
of streamed stdout already delivered to the consumer.
It is not a daemon-side container timeout: it does not stop a started or
detached container or guarantee cancellation of the command inside it. See
[Client, setup, and errors](client-and-errors.md) for the default-deadline
contract. Image pull/inspect preflights and detached-result inspection are
outside the selected run/execute/logs launch budget; a configured client
default still applies separately to those launches. `logs` inspects the
container before its logs CLI starts, outside the deadline. Invalid `timeout`
values are rejected before that inspection; with a valid timeout, a missing
container raises `NoSuchContainer` before log collection.

`wait` (`docker.wait(x, *, timeout=None)`, also `docker.container.wait`) blocks
until the selected containers stop, so give anything that may keep running a
deadline. It validates `timeout` before consuming the input: an invalid value
raises `ValueError` even for `[]`, while a valid one with `[]` returns `[]`
without a launch. A single reference returns an `int`, an iterable of references
a `list[int]`. Expiry raises `DockerTimeout` without stopping the containers.

For running/healthy readiness plus designated successful one-shot completion,
use `capture_containers()` and `wait()` from `pywhales.convergence`, not
`docker.wait()`. They return typed observation outcomes instead of exit codes;
set `fail_on_exit=True` at capture to fail when a long-running member exits
without restarting, even with exit code 0, or `fail_on_unhealthy=True` to fail on
an eligible running member reporting `unhealthy`. Both default to `False`. See
[Standalone containers](observation-and-convergence.md#standalone-containers)
for restart-budget precedence and designated completion rules.

`start` takes `timeout` in every mode (unattached, attached and captured, or
streamed) and through all three entry points, validated before the container
iterable is consumed. An invalid value raises `ValueError` even for `[]`, while
a valid one with `[]` returns `None` without a launch. A streamed `start`,
`run`, `execute`, or `logs` deadline keeps advancing while nobody iterates, so
exhaust the stream or close it on early exit; closing reaps only the local CLI.

## Bound Prune CLI Launches

Container, image, network, volume, and system `prune()` accept keyword-only
`timeout=None`. Give each cleanup call its own positive finite seconds;
`None` inherits the client's launch-snapshotted default, and an explicit value
replaces it without changing shared configuration. Validation rejects booleans,
zero, negative and nonfinite values before filters are consumed or the executable
is resolved. This is a local CLI deadline, not an object-age `until` filter or a
Docker timeout flag.

| Call | Result |
| --- | --- |
| `client.container.prune(..., timeout=10)` | `None` |
| `client.container.prune(..., stream_logs=True, timeout=10)` | Owned `ProcessStream` of `(source, bytes)` |
| `client.image.prune(..., timeout=10)` | `str` CLI output |
| `client.network.prune(..., timeout=10)` | `None` |
| `client.volume.prune(..., timeout=10)` | `None` |
| `client.system.prune(..., timeout=10)` | `None` |

Use the explicit command-group methods, not an assumed root `prune` alias.
Keep each method's flags and filter grammar: system accepts a mapping or pairs,
while the other four require iterable pairs and reject mappings. Repeated pairs
retain order. Check installed signatures before selecting filters or options.

Container streaming starts eagerly and its deadline advances even before the
first iteration. Exhaust it or use a context manager; early close is idempotent,
and a recorded timeout is delivered once after local child/pipe/thread cleanup.
Retained partial output does not replay streamed stdout already delivered.
Every prune suppresses confirmation and deletes resources: run only the cleanup
the user authorized. A timeout kills and reaps the local CLI, but doesn't prove
daemon-side removal stopped or rolled back. Do not automatically retry a prune
after an ambiguous timeout.

## Bound Stream Buffering

Set `DockerClient(stream_buffer_bytes=1_048_576)` to bound buffered output at
shared process-stream boundaries, including logs collected into text. The
default `None` is unbounded; a bound must be a positive integer, not a boolean.
This is a client option, not a `run(..., buffer_bytes=...)` argument.

The stdout/stderr payload queue shares one budget. Each unfinished pipe record
and retained stderr history has a separate bound, so this isn't a total-memory
cap. Queue pressure blocks producers rather than dropping output; a record
(including its newline) or stderr history exceeding its bound raises
`BufferError`. For pull/push progress, the independent
`image_stream_buffer_bytes` override falls back to this general budget when
`None`; see [Registry transfers and archives](registry-and-archives.md).

For a total across captured output, stream/archive reads and managed stdin
writes, use `InspectionPolicy.max_total_io_bytes` inside `observation()`. It
doesn't replace this buffer setting or the separate response, stream and stdin
caps. Raw stream reads, including read-ahead, count before consumer decoding;
direct/inherited stream stdin is excluded. See
[Sharing one IO budget](observation-and-convergence.md#sharing-one-io-budget).

## Own Detached Containers and Streams Separately

```python
from pywhales import docker

with docker.run("alpine:3.22", ["sleep", "300"], detach=True) as container:
    print(docker.execute(container, ["echo", "ready"]))
    with docker.logs(container, tail=20, stream=True) as logs:
        for source, chunk in logs:
            print(f"{source}: {chunk.decode(errors='replace')}", end="")
```

The outer context owns a newly created disposable container. On exit it stops
the container if still running, then removes it with `volumes=True` unless
Docker auto-removal applies. Don't use this context pattern around an existing
container returned by inspect or list: exiting would remove someone else's
resource. Keep persistent data out of this disposable pattern.

The inner context owns only the local CLI process, pipes, and reader threads.
Consume the stream to exhaustion to observe a nonzero command exit as
`DockerException`. Exiting early or calling `close()` cancels and reaps the
local CLI; that cancellation isn't proof the command succeeded and doesn't
remove daemon-managed containers. A bare loop `break` doesn't close a stream;
a dropped stream is closed only after garbage collection, so close it yourself.
Recorded deadline, observation, or reader errors remain deliverable once after
`close()` or on a clean context exit; cleanup preserves an active body exception.

`follow=True` can keep log collection open indefinitely. Prefer a snapshot or
bounded `tail` unless following is requested; if following, provide a deliberate
cancellation boundary and keep the stream in a context manager.

Container properties such as `name` and `state` can issue inspect calls and use
a short-lived cache. Read a property once into a local variable for a coherent
decision; use `container.reload()` when an explicit refresh is required.
A reload must still describe the handle's immutable ID; a mismatched snapshot
raises `ParsingError` and leaves the cached data and refresh time untouched.

## Copy Files or Export a Filesystem

`docker.copy(source, destination, *, archive=False, follow_link=False,
quiet=False, timeout=None)` accepts a local path and a
`(container, path_in_container)` endpoint, in either direction. Local paths refer
to the Python host, unlike bind mounts. It returns `None` and doesn't accept
byte iterators. `Container.copy_from()` and `copy_to()` forward the same
copy flags and keyword-only local deadline.

`docker.export(container, output, *, timeout=None)` writes the container
filesystem to a local tar path and returns `None`; `Container.export()` takes
the output path and the same deadline. It doesn't return archive chunks and
isn't an image backup. For image archives, use
[save and load](registry-and-archives.md#save-and-load).

## Docker CLI 29 Container Changes

Container `create` and `run` no longer accept `content_trust` or
`kernel_memory`, `update` no longer accepts `kernel_memory`, and `run` no longer
accepts `service_ports`. Passing one raises `TypeError`. The `trust` command group is gone too, because Docker CLI 29 removed
`docker trust`; see
[Docker CLI 29 surface changes](client-and-errors.md#docker-cli-29-surface-changes).

`create` and `run` gained keyword-only `annotations` (a mapping), `cpu_count`,
`cpu_percent`, `io_maxbandwidth`, `io_maxiops`, `umask`, `use_api_socket`, and
`quiet`. `run` also takes `detach_keys` and `attach`, an iterable of `"stdin"`,
`"stdout"`, and `"stderr"`. Other methods changed as follows:

| Method | Change |
| --- | --- |
| `commit` | Keyword-only `changes` (a string or iterable of Dockerfile instructions); `pause=False` now emits `--no-pause` |
| `docker.copy`, `Container.copy_from`, `Container.copy_to` | Keyword-only `archive`, `follow_link`, and `quiet`, all `False` by default |
| `inspect` | Keyword-only `size=False` forwards `--size` |
| `list` | Keyword-only `last` (an `int`) and `latest` |
| `remove` | Keyword-only `link=False` forwards `--link` |
| `stop`, `restart` | Keyword-only `signal`; the existing `time` argument now emits `--timeout` instead of the deprecated `--time` |
| `start` | Keyword-only `checkpoint` and `checkpoint_dir` |

### Published ports

`container.port(container, private_port=None, protocol=None)` runs
`docker port` and returns `list[PublishedPort]`. `Container.port()` and the root
alias `docker.port` mirror it; the handle method omits the `container` argument:

```python
from pywhales import DockerClient

docker = DockerClient()
# `container` is a running Container with a published port.
for mapping in docker.port(container, 80, "tcp"):
    print(mapping.host_ip, mapping.host_port)
```

`PublishedPort` is a frozen dataclass in
`pywhales.components.container.cli_wrapper` with `container_port` (`str`),
`host_ip` (`str`), and `host_port` (`int`).

### Checkpoints

`docker.checkpoint` is a command group on the client:

```python
from pywhales import DockerClient

docker = DockerClient()
docker.checkpoint.create("job", "cp1", leave_running=True)
print(docker.checkpoint.list("job"))  # list[str]
docker.checkpoint.remove("job", "cp1")
```

`create(container, checkpoint, *, checkpoint_dir=None, leave_running=False,
timeout=None)` stops the container after the checkpoint unless
`leave_running=True`. Its `timeout` is a local CLI deadline.
`list(container, *, checkpoint_dir=None)` returns checkpoint names and
`remove(container, checkpoint, *, checkpoint_dir=None)` returns `None`. Restore
by passing `checkpoint=` (and optionally `checkpoint_dir=`) to `start`.
Checkpointing needs an experimental daemon with CRIU; without them these calls
raise `DockerException`, so don't treat the group's presence as proof the engine
supports it.
