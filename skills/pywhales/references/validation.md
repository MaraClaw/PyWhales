# Usage Validation

## Inspect the Installed Package First

Use the application's Python environment. This works outside a PyWhales
checkout and doesn't invoke Docker:

```python
from importlib.metadata import version
from inspect import signature

from pywhales import DockerClient, docker

print(version("pywhales"))
print(signature(DockerClient))
print(signature(docker.compose.up))
help(docker.compose.up)
```

Select only the methods needed by the task. `signature` establishes supported
keywords and help text explains intent. Match these checks to the output and
lifecycle guidance in the relevant reference rather than copying an example
from another Docker library. Client construction and introspection don't
establish daemon or plugin availability.

The guidance describes current source APIs with project version 1.1.2.
An installed version string alone isn't proof of support. Check environment
inheritance, standalone targets, completion and restart keywords, stdin policy
fields, inventory kinds, and typed stream returns
before copying their examples:

```python
from annotationlib import Format, get_annotations
from dataclasses import fields
from inspect import signature
from typing import get_args, get_overloads

from pywhales import DockerClient, InspectionPolicy, docker
from pywhales.client_config import ClientConfig
from pywhales.convergence import (
    ComposeTarget,
    ContainerTarget,
    DeadlineDetail,
    ServiceTarget,
    capture_compose,
    capture_containers,
    wait as wait_for_convergence,
)
from pywhales.inventory import InventoryKind

print(signature(capture_compose, annotation_format=Format.STRING))
print(signature(capture_containers, annotation_format=Format.STRING))
print(signature(wait_for_convergence, annotation_format=Format.STRING))
print([f.name for f in fields(ContainerTarget)])
print(signature(InspectionPolicy))  # max_total_stdin_bytes: int | None = None
print({f.name: f.default for f in fields(ComposeTarget)}["max_restarts"])
detail_defaults = {f.name: f.default for f in fields(DeadlineDetail)}
print(detail_defaults["restart_budget_exceeded_containers"])
print(get_args(InventoryKind.__value__))  # must include "manifest_list"
print(signature(DockerClient).parameters["strict_env_transport"])  # bool = False
print({f.name: f.default for f in fields(ClientConfig)}["strict_env_transport"])
print(signature(DockerClient).parameters["inherit_environment"])  # bool = True
print({f.name: f.default for f in fields(ClientConfig)}["inherit_environment"])
print(get_annotations(ServiceTarget, format=Format.STRING)["mode"])  # job modes?

methods = [
    getattr(docker.compose, name)
    for name in (
        "build",
        "create",
        "down",
        "execute",
        "logs",
        "pull",
        "run",
        "start",
        "stop",
        "up",
    )
]
methods += [
    docker.container.run,
    docker.container.wait,
    docker.container.prune,
    docker.service.logs,
]
for method in methods:
    print(method.__qualname__, signature(method, annotation_format=Format.STRING))
    for overload in get_overloads(method):
        print(signature(overload, annotation_format=Format.STRING))
```

Look for `completion_services: tuple[str, ...] = ()` and `ProcessStream` in the
matching streaming overloads. Also look for:

- `max_total_stdin_bytes: int | None = None` on `InspectionPolicy`: a positive
  value enables bounded captured input and accounts archive uploads separately
  from response and stream output. `None` does not enable uncapped observed input.
- `max_restarts: int | None = None` on `capture_compose` and `ComposeTarget`, and
  a default-zero `DeadlineDetail.restart_budget_exceeded_containers` field.
  This budget accepts zero, applies only to selected long-running services, and
  blocks convergence without creating a new terminal failure reason.
- `strict_env_transport: bool = False` on `DockerClient` and on `ClientConfig`
  (import the latter from `pywhales.client_config`, not the package root).
- `inherit_environment: bool = True` on both client types; false restricts the
  child base before overlays, independently of strict assignment transport.
- `capture_containers(client, references, *, completion=(), max_restarts=None,
  policy, timeout, cancel=None)` and `ContainerTarget` exported from
  `pywhales.convergence`; the target has `container_ids`, `completion`, and
  `max_restarts`. The convergence `wait` annotation must accept that target.
  This is separate from the exit-code `docker.container.wait` API.
- A keyword-only `timeout: float | None = None` on Compose `build` and `execute`,
  and on `container.wait`, whose overloads return `int` for one reference and
  `list[int]` for an iterable.
- `replicated_job` and `global_job` in the `ServiceTarget.mode` annotation;
  `help(ServiceTarget)` shows it too. `capture_service` has no job-specific
  parameter, so its signature and the version string can't establish Swarm job
  support. Check the target annotation and the installed package's matching
  documentation; annotations alone don't prove runtime behavior.

Optional `None` results still need a guard before context entry. A missing
import, keyword, kind, or annotation means this installed capability isn't
established. Introspection doesn't prove environment-forwarding
behavior merely because `envs` exists: check its installed help and matching
package documentation too. The empty-pull example in
[Registry transfers and archives](registry-and-archives.md#pull-and-push) checks
context management without locating or launching Docker.

## Check the Matching Surface

1. Verify imports and the installed version without Docker. Parse or compile
   generated Python to catch syntax errors without running its Docker calls.
2. When engine access is authorized, check the selected target with
   `docker.version()` and check only the required CLI plugins.
3. Exercise the requested operation with owned disposable resources. For a
   foreground smoke check, use
   `docker.run("alpine:3.22", ["echo", "ok"], remove=True)` and expect `"ok"`.
   This can pull an image; don't run it under a no-Docker or no-network constraint.
4. Check the contract that matters: text content, detached object, exhausted
   stream, exported files, or a local image. Add a deliberate nonzero command
   such as `["false"]` only in an authorized disposable run to check error handling.
5. Report checks performed separately from checks blocked by unavailable
   engines, credentials, builders, or plugin versions. Don't call static
   signature checks an integration test.

For builds, validate the chosen exporter instead of assuming an `Image`.
For Compose, use a dedicated project and preserve persistent volumes unless
their deletion is requested. Bound log collection and release local streams
even when a reader stops early.

Verify the user's application or script, not the PyWhales repository's
contributor tests. A version string alone doesn't establish supported arguments
or return contracts.

Checking `default_timeout`, `telemetry_observer`, `environment`,
`strict_env_transport`, or `inherit_environment` follows the same signature-first rule: confirm it in
`signature(DockerClient)` before assuming it is supported. Opt-in admission
(`pywhales.admission`), bounded observation (the root `observation`,
`capture_inventory`, and `InspectionPolicy` exports, plus `bulk_reload` from
`pywhales.client_config`), and convergence (`pywhales.convergence`, covering
Swarm services/jobs/stacks, Compose projects, and standalone containers) are separate imports that don't
change default `DockerClient` behavior; verify their presence the same way
before building on them. `pywhales.reconciliation`, `pywhales.profiles`, and
`pywhales.storage` are likewise opt-in imports that the package root doesn't
re-export.

If `signature()` raises `NameError` on an annotation that is imported only for
type checking, as it does for `bulk_reload`'s `InspectionPolicy`, pass
`annotation_format=annotationlib.Format.FORWARDREF` (or `Format.STRING`) after
`import annotationlib`.
