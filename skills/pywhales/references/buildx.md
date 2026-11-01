# Buildx

`docker.build` is an alias of `docker.buildx.build`, not a legacy builder or a
Docker SDK method. A Buildx plugin, suitable builder, and build context are
required. Examples below assume the working directory contains a Dockerfile.

## Build for Local Use

```python
from pywhales import docker

image = docker.build(
    ".",
    tags="example-app:dev",
    load=True,
    progress="plain",
)
print(image)
```

Use `load=True` when the result must be available in the local daemon. Keep a
tag when using the `docker-container` driver so PyWhales can recover the loaded
image. Without an explicit exporter, loading depends on the builder's driver.

| Intent | Arguments |
| --- | --- |
| Choose Dockerfile/stage | `file="Dockerfile.dev", target="test"` |
| Pass build arguments | `build_args={"VERSION": "dev"}`; see [Build Arguments](#build-arguments) |
| Disable cache | `cache=False`, not `no_cache=True` |
| Select target platforms | `platforms=["linux/amd64", "linux/arm64"]` |
| Export files | `output={"type": "local", "dest": "./dist"}` |
| Push to a registry | `push=True, tags="registry.example.com/team/app:release"` |

Choose an exporter for the requested result. Only push to a destination the
task authorizes, with existing registry authentication. Multi-platform builds
need a capable builder/exporter; don't assume the local image store can load
every multi-platform result.

## Build Arguments

An eligible `build_args` name reaches Buildx as name-only `--build-arg VERSION`,
and its value travels in that launch's child environment. This holds in every
build branch: blocking builds, `stream_logs=True`, `call=` reports, and both load
paths (Docker driver and other drivers). It doesn't change the parent Python
environment.

Names that need the literal form stay `--build-arg KEY=VALUE` by default. That
covers reserved CLI or runtime names (`DOCKER_*`, `BUILDKIT_*`, `PATH`, and
anything starting with `PYTHON`, matched case-insensitively), invalid entries
(empty or `=`/NUL-containing names, NUL-containing values, non-string keys),
duplicate names, and names colliding with the effective environment. With
`DockerClient(strict_env_transport=True)` any such entry raises a value-free
`ValueError` instead. The check runs before executable lookup or builder
inspection, so no launch happens for that call.

`PYTHON_VERSION` is reserved on purpose, because the `PYTHON` prefix is treated
as CLI-controlling. A Dockerfile `ARG PYTHON_VERSION` therefore works only in
the default literal mode; under strict mode rename the `ARG`, for example to
`PY_VERSION`.

Build args aren't a secret vault in either mode: values can still appear in
image history and provenance, and in argv when a literal fallback applies. Use
`secrets=` for sensitive build input. `bake(set=...)` isn't affected by strict
mode and keeps its `--set` arguments. `variables=` keeps overlaying the Bake
subprocess environment as before.

`build`, `bake`, and `prune` accept a keyword-only `timeout` in positive finite
seconds, replacing the client's `default_timeout` for that one local CLI
launch; `None` inherits it. Builder inspection before launch and image
inspection after completion fall outside this budget, and each of `bake`'s
build/follow-up-config calls gets its own fresh budget rather than one shared
deadline. A streaming deadline keeps advancing before and between iterations.
Expiry raises `DockerTimeout` without guaranteeing that the daemon-side build
stops. See [Prune Build Cache](#prune-build-cache) for `prune`.

## Return Contracts

| Mode | Result |
| --- | --- |
| Blocking normal build with an identifiable locally loaded image | `Image` |
| Blocking build with push/nonlocal export and no local load | `None` |
| Blocking `call="check"`, `"outline"`, or `"targets"` | Captured report `str` |
| `stream_logs=True` | Owned `MappedProcessStream[str]`, not an image |

Don't access `.id` on an arbitrary build result. A successful build can return
`None`. Report calls bypass image recovery, and consuming a log stream doesn't
produce a final `Image` return value.

A normal blocking `bake()` returns `BakeResult` configuration by default: it
builds, then runs a separate `--print` query. That query isn't a build-time
snapshot. `return_config=False` skips it and returns `None`; `print=True` only
queries configuration and conflicts with streaming, report calls, and
`return_config=False`. Bake reports return `str`, and Bake streams yield decoded
`str`; neither performs the follow-up query. `variables={...}` overlays the
Bake subprocess environment on each invocation.

## Stream Decoded Build Logs

```python
from pywhales import docker

with docker.buildx.build(
    ".",
    tags="example-app:dev",
    load=True,
    progress="plain",
    stream_logs=True,
    timeout=1800,
) as lines:
    for line in lines:
        print(line, end="")
```

These items are already `str`, unlike container and Compose `(source, bytes)`
streams. The `timeout` bounds only the local build CLI, and its clock keeps
running while you aren't iterating. Drain the stream to detect CLI failure and
use its context manager for early-exit cleanup. `stream_logs=True` rejects
`progress=False`, `"tty"`, `"none"`, and `"quiet"`; use `"plain"` for readable
automation logs.

## Check Without Building an Image

```python
from pywhales import docker

report = docker.buildx.build(".", call="check")
print(report)
```

`call="outline"` and `call="targets"` also return report text in blocking mode.
These frontend methods require Buildx support for `--call`; they aren't a
generic offline or side-effect-free sandbox. Check installed signatures and
plugin support before using newer options such as `no_cache_filter` and
`annotations`.

## Prune Build Cache

`docker.buildx.prune(all=False, filters=..., stream_logs=False, *, timeout=None)`
removes build cache on the current builder. It always passes `--force`, which
suppresses Docker's confirmation prompt, so require explicit deletion
authorization; request `all=True` or broad filters only when the task asks for
them. `filters` is a mapping such as
`{"until": "24h"}` or an iterable of `(key, value)` string pairs. A string,
bytes, or an iterable holding anything but string pairs raises `TypeError`
before the executable is looked up.

A blocking call returns `None` after the CLI completes. `stream_logs=True`
returns an owned `MappedProcessStream[str]` of decoded text, with the CLI
started during the call: drain it or use its context manager on early exit.
Closing it reaps the local CLI and doesn't undo cache removal.

`timeout` is a local deadline only: positive finite seconds from the CLI's
launch, replacing the client default, with `None` inheriting it; it is checked
before `filters` is read. It is never forwarded as Buildx's own
`prune --timeout` flag, so it doesn't change what Buildx removes. A streamed
deadline keeps advancing while consumption pauses. Expiry kills and reaps the
local CLI and raises `DockerTimeout` without undoing cache removal.

## Builders In Inventory Captures

Builders are a supported kind for `capture_inventory()` and `bulk_reload()`.
Pass `InventoryRequest(kind="builder", references=(name,), client_config=...)`
or an existing `Builder` handle. Limits, deadlines, and cancellation come from
`observation()`; see
[Observation budgets and convergence](observation-and-convergence.md).

- References are exact builder names, not IDs, and no name reaches the command
  line. Buildx has no batch inspect, so each client configuration group runs
  one `buildx ls --format "{{json . }}"` listing. Inventory capture returns one
  detached record per requested occurrence, in request order, duplicates included;
  `bulk_reload()` instead stages snapshots and refreshes the original handles,
  returning `None`.
- That listing is parsed strictly. Lines identical byte for byte collapse,
  because node rows repeat them. A non-builder line, a null or empty name, or
  two different lines under one name raises `ParsingError` before any snapshot
  returns, as does a requested name missing from the listing. Names appear in
  that error only with `raw_diagnostics=True`.
- Public `docker.buildx.list()` is unchanged and separate: it keeps its lenient
  parsing, existing dedupe, and unspecified order. Don't expect the strict
  contract from it, and don't rely on its ordering.
