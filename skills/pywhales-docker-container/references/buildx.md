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

Collision checks use the frozen child base selected by
`inherited_environment_names`, or legacy `inherit_environment` when names are
unset. Excluded ordinary ambient names don't collide; selected names and explicit
client overlays still do, and reserved names remain reserved. Mandatory Buildx
`DOCKER_CLI_EXPERIMENTAL=enabled` applies after overlays even with empty inherited
names. See [CLI environment isolation](client-and-errors.md#isolate-the-cli-environment).

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

A local deadline kills and reaps only the local `docker` CLI. The `docker-buildx`
plugin child it started can outlive it, as live checks showed for `dap.build` and
`history.trace`, so don't treat expiry as proof the plugin stopped.

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

Space-based pruning uses the keyword-only `max_used_space`, `min_free_space`, and
`reserved_space` (an `int` or `str`), plus `verbose`. Buildx's own `--timeout` is
`status_timeout`, covered below.

## Buildx 0.39 Options

`build` also accepts keyword-only `cgroup_parent`, `policies`, `quiet`,
`resources`, `shm_size`, and `ulimits`; `quiet=True` rejects `stream_logs=True`.
`bake` gains `allow`, `policies`, `provenance`, `sbom`, `list`, and
`variables_set`. `list` is `"targets"` or `"variables"` and returns report text.
`variables_set` forwards `--var`, which is separate from `variables=`, the
Bake subprocess environment overlay.

`create(config=...)` now emits `--buildkitd-config`, and `create` also takes
`leave`, `node`, and `status_timeout`. `remove` makes its builder optional and
adds `all_inactive`, `force`, `keep_daemon`, and `keep_state`. Removing builders
deletes them, so require explicit authorization as for prune.

Docker's own builder `--timeout` is `status_timeout` (an `int` or `timedelta`) on
`list`, `inspect`, `create`, `prune`, `remove`, and `disk_usage`. `timeout`
never means that flag; it's always the local CLI deadline.

`disk_usage(*, filters=None, builder=None, status_timeout=None, verbose=False,
debug=False, timeout=None)` normally returns `list[BuildxDiskUsage]`. `verbose=True`
returns the CLI's text as `str`.

Every Buildx method accepts keyword-only `debug`. On the methods that parse
output (`list`, `inspect`, `disk_usage`, `history.list`, `history.inspect`, and
`imagetools.inspect`), `debug=True` returns a `str`: stdout, a newline, then the
stderr debug log. See [Docker CLI 29 surface changes](client-and-errors.md#docker-cli-29-surface-changes)
for the shared raw-text rule.

`buildx.dial_stdio(...)` and `buildx.dap.build(...)` inherit the caller's stdio,
block until the CLI exits, and return `None`; neither captures output. Give them
a `timeout` unless a blocking call is intended.

`imagetools.create` adds `prefer_index`, `metadata_file`, `platforms`, and
`progress`.

## History, Policy, And Replay

`docker.buildx.history` wraps `buildx history`. `list` returns
`list[BuildRecord]`, `inspect` returns `BuildRecordDetail`, `inspect_attachment`
and `logs` return `str`, `import_` returns the imported records'
`docker-desktop://` dashboard URLs as `list[str]`, `export`, `open`, and
`remove` return `None`, and `trace` returns an owned `ProcessStream`. Each takes `builder` and `debug`;
`logs`, `export`, `import_`, `open`, and `trace` also take a local `timeout`.
`export` requires a keyword-only `output` path.

```python
from pywhales import DockerClient

docker = DockerClient()
for record in docker.buildx.history.list(local=True):
    print(record)
```

`history.open()` opens a builder record in Docker Desktop's build UI, and
`history.import_()` uploads bundles to Docker Desktop's `.imported` store and
opens the first one, so call them only where that's intended. Imported records
don't appear in `history.list()` and can't be passed to `open()`.
`history.remove(all=True)` deletes every record: require explicit authorization.
Use `trace()` as a context manager. In compare mode the buildx plugin may keep
serving after you close it, because closing reaps only the local CLI.

`docker.buildx.policy.eval(source, *, file=None, platform=None, fields=None,
print=False, ...)` and `docker.buildx.policy.test(path, *, filename=None,
run=None, ...)` return the CLI's text as `str`.

`docker.buildx.replay.build(subject, ...)` returns a plan `dict` with
`dry_run=True`, an owned `MappedProcessStream[str]` with `stream_logs=True`, and
`None` otherwise.

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
