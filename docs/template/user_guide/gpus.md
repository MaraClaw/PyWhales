# GPUs, vGPU and CDI devices

GPU checks in pywhales look at the Docker daemon the client targets (for a Swarm
service, the nodes of its Swarm), never at the machine running Python. A client
built for a remote GPU server reads that server's capabilities from your laptop:

```python
from pywhales import DockerClient

gpu_host = DockerClient(host="ssh://ops@gpu-01.example")  # or context="gpu-01"
support = gpu_host.system.gpu_support()
print(support.gpus_available, support.gpu_devices)
```

`gpu_support()` makes a single `docker system info` call. It reports:

- `gpu_runtimes`: a registered `nvidia` runtime, meaning the NVIDIA Container
  Toolkit is installed.
- `cdi_spec_dirs` and `cdi_devices`: the CDI specification directories and the
  devices the daemon discovered (Docker 28 and later). `cdi_devices` is `None` on
  older daemons. That means "unknown", not "no devices".
- `gpu_devices`: the discovered CDI devices of a kind that `--gpus` can inject,
  which are `nvidia.com/gpu` and `amd.com/gpu`.
- `whole_gpus`, `gpu_uuids` and `mig_devices`: the `gpu_devices` grouped by name.
  Those are bare indexes (`nvidia.com/gpu=0`), `GPU-<uuid>` names, and MIG
  partitions (`0:1` or `MIG-<uuid>`). A card usually appears under both its index
  and its UUID.
- `gpu_count`: the number of `whole_gpus`, or `None` when `cdi_devices` is `None`.
  It's 0 on a host that names its GPUs only by UUID. MIG partitions and aliases
  such as `gpu0` and `all` aren't counted.

NVIDIA vGPU guests and MIG partitions show up as ordinary `nvidia.com/gpu`
devices, for example `nvidia.com/gpu=0`, `nvidia.com/gpu=0:1` (MIG) or
`nvidia.com/gpu=GPU-<uuid>`. On a daemon that reports two GPUs and one MIG
partition:

```python
print(support.whole_gpus)  # ('nvidia.com/gpu=0', 'nvidia.com/gpu=1')
print(support.mig_devices)  # ('nvidia.com/gpu=0:1',)
print(support.gpu_count)  # 2
```

## Requesting a number of vGPUs

Each vGPU the hypervisor assigned to the VM is one GPU to Docker. There are three
ways to request N of them:

```python
image = "nvidia/cuda:13.0.2-base-ubuntu24.04"
cmd = ["nvidia-smi", "-L"]

gpu_host.run(image, cmd, gpus=2, remove=True)  # --gpus 2: any two GPUs
gpu_host.run(image, cmd, gpus='"device=0,1"', remove=True)  # exactly GPUs 0 and 1
gpu_host.run(
    image, cmd, devices=["nvidia.com/gpu=0", "nvidia.com/gpu=1"], remove=True
)  # one CDI device per name
```

`gpus` accepts an `int` and passes it unchanged to `--gpus`. For a list of
devices, keep the inner double quotes: there's no shell to strip them, and Docker
parses the value as CSV, so `device=0,1` alone would split at the comma. Docker
hands out whole GPUs, so MIG slices never count toward `gpus=N`; request a slice
by its CDI name instead.

Two limits come from the hypervisor, not from Docker or pywhales:

- A container can't get more vGPUs than were assigned to the VM.
- There's no fractional request. The size of each vGPU (for example 8 GB) is
  fixed by its vGPU profile when the VM is created.

`require_gpu=True` checks a count against the names the daemon reports (see
below). To count the GPUs a host offers, read `support.gpu_count`.

## Checking before a run

`run()` and `create()` take `require_gpu=True`. It probes the daemon before the
image is pulled, so a missing driver or GPU fails fast instead of after
downloading a multi-gigabyte CUDA image:

```python
from pywhales.exceptions import GpuSupportError

try:
    gpu_host.run(
        "nvidia/cuda:13.0.2-base-ubuntu24.04",
        ["nvidia-smi", "-L"],
        gpus="all",
        require_gpu=True,
        remove=True,
    )
except GpuSupportError as error:
    print(error.reason, error.devices)
```

Pass CDI device names through `devices` to request a specific vGPU or MIG slice:
`devices=["nvidia.com/gpu=0:1"], require_gpu=True`. Each name must match one of
the daemon's discovered devices exactly. Host device paths such as `/dev/fuse`
aren't checked.

The preflight checks counts and selections too. Docker's CDI path turns
`--gpus N` into the literal names `nvidia.com/gpu=0` through
`nvidia.com/gpu=<N-1>`, so `gpus=4` on a VM with one vGPU is refused before
anything is pulled:

```python
try:
    gpu_host.run(image, cmd, gpus=4, require_gpu=True, remove=True)
except GpuSupportError as error:
    print(error.reason, error.devices)  # gpu_device_missing ('nvidia.com/gpu=1',)
```

A selection such as `'"device=0,2"'` or `device=GPU-<uuid>` resolves to
`nvidia.com/gpu=0`, `=2` or `=GPU-<uuid>` the same way. Some requests pass
without being verified:

- `gpus="all"` is never count-checked.
- MIG names in a count or selection (`0:1` indexes and `MIG-<uuid>` names) aren't
  verified, because MIG layouts change at runtime and Docker falls back to the
  runtime hook for them. An explicit CDI name such as
  `devices=["nvidia.com/gpu=0:1"]` is different: it has to match a discovered
  device exactly.
- A resolved name is verified only when the daemon reports at least one name of
  the same kind and naming family (whole index or `GPU-<uuid>`). A UUID request
  on a daemon that reports only indexes passes, and so does an index request on
  a daemon that reports only MIG partitions.
- A host with an `nvidia` runtime that reports no CDI devices passes any count,
  because there are no names to compare.

Docker's NVIDIA driver tries CDI first and falls back to the legacy runtime hook
when CDI fails for any reason. A `gpu_device_missing` refusal therefore assumes
the host's CDI specification is current: regenerate it with
`nvidia-ctk cdi generate` after you change the GPUs of a host.

`error.devices` lists the missing indexes only up to the first one past the
highest reported index, so a huge count stays short.

`support.check(gpus=2, devices=[...])` runs the same check on a `GpuSupport` you
already hold and raises the same `GpuSupportError`, with no launch of its own.
`check_requests` takes parsed `GpuRequest` values instead, and
`GpuRequest.parse` builds one from a `--gpus` value. All three live in
`pywhales.components.system.gpu`, which the package root doesn't export.

The `GpuSupportError.reason` values are listed in the order the daemon checks
run. `no_node_capacity` comes only from the Swarm service check below.

| Reason | Meaning |
| --- | --- |
| `not_linux` | The daemon doesn't run Linux containers. |
| `no_gpu_driver` | `gpus` was requested, but the daemon reports neither an `nvidia` runtime nor a GPU CDI device. |
| `cdi_devices_unreported` | The daemon predates the discovered-device list, so CDI names can't be verified. |
| `cdi_device_missing` | Some explicitly requested CDI names aren't discovered. `devices` lists them. |
| `gpu_device_missing` | A `gpus` count or device selection resolves to GPU names the daemon doesn't list, never MIG names. `devices` lists them. |
| `no_node_capacity` | No ready, active Swarm node advertises enough of a requested generic resource. `devices` lists the `kind=value` requests, such as `gpu=2`. |

Every `require_gpu` check is opt-in, and none is a guarantee: Docker still decides
when the container starts. The container check costs one extra CLI launch, and its
evidence is incomplete: a host that serves `--gpus` only through the toolkit hook,
with no registered `nvidia` runtime and no CDI specification, reports
`no_gpu_driver` even though a real run would succeed. On such hosts, leave
`require_gpu` off.

## Checking a Swarm service

Swarm services ask for GPUs through generic resources, not `gpus`.
`service.create()` takes `require_gpu=True` and checks the nodes of the Swarm
before it creates anything:

```python
swarm = DockerClient(host="ssh://ops@manager-01.example")
try:
    swarm.service.create(
        "nvidia/cuda:13.0.2-base-ubuntu24.04",
        ["nvidia-smi", "-L"],
        name="gpu-job",
        generic_resources=["gpu=2"],
        require_gpu=True,
    )
except GpuSupportError as error:
    print(error.reason, error.devices)  # no_node_capacity ('gpu=2',)
```

The nodes have to advertise the resource first; see
[Docker Swarm generic resources](generic_resources.md). The preflight runs after
the `timeout` and option validation and before the single `service create`
launch. Without a nonzero `kind=int` request in `generic_resources`, it raises
`ValueError` and launches nothing. Otherwise it lists the nodes of the selected
manager (`node ls` plus one batched `node inspect`), outside `timeout`, and
compares them:

- One ready, active node has to advertise enough of every request. A node's
  capacity per kind is its discrete values plus the number of its named
  resources. Requests aren't summed across nodes, and drained, paused or
  not-ready nodes are ignored.
- Placement constraints and the capacity that running tasks already claim are
  ignored. The check can pass a request that then stays pending, but it never
  refuses one that a ready, active node advertises.
- Requests for the same kind are summed here, although the Docker CLI rejects
  duplicate kinds, and an amount may carry a leading `+`.
- A refusal raises `no_node_capacity`, with the unsatisfied requests in
  `devices`. On success the `service create` argv is unchanged.

`generic_resource_requests(requests)` and `check_generic_resources(requests, nodes)`
in `pywhales.components.system.gpu` run the same comparison on node handles you
pass in. Pass hydrated handles, for example from `swarm.node.list(hydrate=True)`,
so that each property read doesn't cost an inspect launch.

## Checking Compose services

`compose.up()` and `compose.run()` take `require_gpu=True` too. They read the
project's normalized configuration, so the Compose file requests GPUs the way it
normally does:

```python
project = DockerClient(host="ssh://ops@gpu-01.example", compose_files=["compose.yaml"])
try:
    project.compose.up(["trainer"], detach=True, require_gpu=True)
except GpuSupportError as error:
    print(error.reason, error.devices)
```

The check reads each service's `gpus`, its `deploy.resources.reservations.devices`
entries that list an NVIDIA or AMD GPU capability (`gpu`, `nvidia`, `compute`,
`compat32`, `graphics`, `utility`, `video`, `display` or `amd`) or use the `nvidia`
or `cdi` driver, and the CDI names in `devices`. Like Compose, it takes a `devices`
entry for a CDI device only when it is a mapping whose `source` and `target` are
the same qualified name, as in the normalized configuration. Other `devices`
entries, such as `/dev/fuse`, are ignored. A reservation with no count and no
device IDs means all GPUs, which isn't count-checked. After that:

- It checks the selected services (every service for `up(None)`) and everything
  they depend on, transitively. `dependencies=False` limits it to the selected
  ones.
- A service that starts no instances is skipped, though what it depends on is
  still checked. `up()` takes the instance count from `scales`, else the service's
  `scale`, else `deploy.replicas`. `run()` always checks its own service, because
  it starts one container for it, but skips a dependency scaled to 0.
- It adds two launches, `compose config` and `docker system info`, with the
  client's default deadline. The `timeout` of `run()` doesn't cover them (`up()`
  has none). `config` names the selected services, so profile-gated services that
  `up` or `run` enables are rendered; a service the project doesn't define makes
  it fail with a `DockerException`.
- With no GPU request in the checked services, it raises `ValueError` after
  `config` and before `info`. `up(services=[])` stays a no-op and doesn't probe.
- It runs after the call's arguments are validated and encoded, just before the
  launch, so a bad argument raises before either probe launches. A refusal is a
  `GpuSupportError` with one of the reasons above except `no_node_capacity`.

`compose_gpu_requests(services)` in `pywhales.components.system.gpu` does the
reading for a normalized `services` mapping and returns `(requests, cdi_names)`,
which `check_requests` accepts.

## Daemon refusals

Without the preflight, the daemon itself rejects GPU requests it can't satisfy.
When stderr is captured (by a detached or streamed `run()`), the rejection is
raised as `GpuUnavailable`, a `DockerException` subclass. A foreground `run()`
forwards stderr to the terminal, so it raises a plain `DockerException`.
`create()` doesn't resolve `--gpus` or CDI devices at all; the daemon refuses
them only when the container starts. Use `require_gpu=True` to catch that before
the container is created.

{{autogenerated}}
