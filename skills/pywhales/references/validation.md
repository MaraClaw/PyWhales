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

The guidance describes current source APIs for upcoming project version 1.1.6.
An installed version string alone isn't proof of support. Check environment
inheritance, node and standalone targets, completion/restart/failure keywords, strict workload
errors, refusal/admission correlation records, class/owner queued-demand and
launch-attempt/child-slot limits, startup health intervals and profile approvals,
optional manager leadership, prune deadlines, stdin policy fields, inventory kinds, and typed stream returns
before copying their examples:

```python
from annotationlib import Format, get_annotations
from dataclasses import fields
from inspect import signature
from typing import get_args, get_overloads

from pywhales import DockerClient, InspectionPolicy, docker
from pywhales.admission import AdmissionOwner, ClassLimit, OutcomeCounts
from pywhales.client_config import ClientConfig
from pywhales.convergence import (
    ComposeTarget,
    ContainerTarget,
    DeadlineDetail,
    Failure,
    NodeTarget,
    ServiceTarget,
    capture_compose,
    capture_containers,
    capture_nodes,
    wait as wait_for_convergence,
)
from pywhales.inventory import InventoryKind
from pywhales.profiles import CONTROLS, CreateRequest, RuntimePolicy, SecurityProfile
from pywhales.reconciliation import ConvergenceSummary
from pywhales.telemetry import (
    AdmissionDecisionRecord,
    AdmissionOutcome,
    CompletionRecord,
    LaunchRecord,
    RefusalRecord,
)

print(signature(capture_compose, annotation_format=Format.STRING))
print(signature(capture_containers, annotation_format=Format.STRING))
print(signature(capture_nodes, annotation_format=Format.STRING))
print(signature(wait_for_convergence, annotation_format=Format.STRING))
print([f.name for f in fields(ContainerTarget)])
print([f.name for f in fields(NodeTarget)])  # includes require_leader
print({f.name: f.default for f in fields(NodeTarget)}["require_leader"])  # False
print(signature(InspectionPolicy))  # launch attempts and max_concurrent_children
print(signature(ClassLimit))  # pending_demand_capacity: int | None = None
print([f.name for f in fields(OutcomeCounts)])  # rejected_pending_demand_full
print(get_args(AdmissionOutcome.__value__))  # both pending_demand_full outcomes
print({f.name: f.default for f in fields(ComposeTarget)}["max_restarts"])
print({f.name: f.default for f in fields(ComposeTarget)}["fail_on_exit"])  # False
print({f.name: f.default for f in fields(ContainerTarget)}["fail_on_exit"])  # False
for target_type in (ComposeTarget, ContainerTarget):
    print(
        {f.name: f.default for f in fields(target_type)}["fail_on_unhealthy"]
    )  # False
print(get_annotations(Failure, format=Format.STRING)["reason"])  # unhealthy
print(get_args(ConvergenceSummary.__value__))  # includes unhealthy
print(signature(CreateRequest))  # health_start_interval: int | None = None
print(signature(SecurityProfile))  # max_health_start_interval: int | None = None
print(signature(RuntimePolicy))  # operator-declared supported_controls
print("health_start_interval" in CONTROLS)  # False: conditional, not baseline
detail_defaults = {f.name: f.default for f in fields(DeadlineDetail)}
print(detail_defaults["restart_budget_exceeded_containers"])
print(detail_defaults["non_ready_nodes"])
print(detail_defaults["unavailable_nodes"])
print(detail_defaults["non_manager_nodes"])
print(detail_defaults["unreachable_managers"])
print(detail_defaults["leader_mismatch_managers"])  # 0
print(get_args(InventoryKind.__value__))  # must include "manifest_list"
print(signature(DockerClient).parameters["strict_env_transport"])  # bool = False
print({f.name: f.default for f in fields(ClientConfig)}["strict_env_transport"])
print(signature(DockerClient).parameters["inherit_environment"])  # bool = True
print({f.name: f.default for f in fields(ClientConfig)}["inherit_environment"])
print(signature(DockerClient).parameters["strict_workload_errors"])  # bool = False
print({f.name: f.default for f in fields(ClientConfig)}["strict_workload_errors"])
print(signature(AdmissionOwner, annotation_format=Format.STRING))  # owner queue cap
print([f.name for f in fields(AdmissionDecisionRecord)])
print([f.name for f in fields(LaunchRecord)])  # includes admission_id
print([f.name for f in fields(RefusalRecord)])  # no execution measurements
for record_type in (
    AdmissionDecisionRecord,
    LaunchRecord,
    CompletionRecord,
    RefusalRecord,
):
    print(
        record_type.__name__,
        {f.name: f.default for f in fields(record_type)}["schema_version"],
    )
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
    docker.create,
    docker.run,
    docker.container.create,
    docker.container.run,
    docker.container.wait,
    docker.container.prune,
    docker.image.prune,
    docker.network.prune,
    docker.volume.prune,
    docker.system.prune,
    docker.service.logs,
]
for method in methods:
    print(method.__qualname__, signature(method, annotation_format=Format.STRING))
    for overload in get_overloads(method):
        print(signature(overload, annotation_format=Format.STRING))
```

Look for `completion_services: tuple[str, ...] = ()` on `capture_compose`, and
`ProcessStream` in matching streaming overloads. Also look for:

- `max_total_launch_attempts: int | None = None` on `InspectionPolicy`: positive
  exact integers bound shared enclosing launch reservations, with no refund after
  accepted preflight. Inventory checks its full plan against remaining allowances
  without reserving it. See [Observation budgets](observation-and-convergence.md#bounding-batch-inspection).
- `max_concurrent_children: int | None = None` on `InspectionPolicy`: positive
  exact integers bound shared enclosing owned-child slots until reap. Nested and
  copied contexts share synchronized budgets. Saturation consumes no attempt;
  failed spawn consumes an attempt but returns the slot. Buffered reaped output
  doesn't retain a slot. Lazy save reserves on first iteration, with separately
  charged eager inspect.
- `pending_demand_capacity: int | None = None` on `ClassLimit`: nonnegative exact
  integers bound queued demand only; zero still permits immediate grants.
  `OutcomeCounts.rejected_pending_demand_full` and `AdmissionOutcome`'s
  `pending_demand_full` must accompany it, not just a new constructor keyword.
- Keyword-only `owner_pending_demand_capacity: int | None = None` on
  `AdmissionOwner`: nonnegative exact integers cap queued demand across classes,
  not active work. Immediate grants bypass it, including zero; FIFO stays
  class-local and earlier class rejection checks take precedence. Check
  `OutcomeCounts.rejected_owner_pending_demand_full` and
  `AdmissionOutcome`'s `owner_pending_demand_full` too.
- `max_total_stdin_bytes: int | None = None` on `InspectionPolicy`: a positive
  value enables bounded captured input and accounts archive uploads separately
  from response and stream output. `None` does not enable uncapped observed input.
- `max_restarts: int | None = None` on `capture_compose` and `ComposeTarget`, and
  a default-zero `DeadlineDetail.restart_budget_exceeded_containers` field.
  This budget accepts zero, applies only to selected long-running services, and
  blocks convergence without creating a new terminal failure reason.
- `strict_env_transport: bool = False` on `DockerClient` and on `ClientConfig`
  (import the latter from `pywhales.client_config`, not the package root).
- `strict_workload_errors: bool = False` on both client types. Confirm matching
  installed documentation describes attached workload failures, not every CLI
  error; detached and preflight specialization remains separate.
- `inherit_environment: bool = True` on both client types; false restricts the
  child base before overlays, independently of strict assignment transport.
- `fail_on_exit: bool = False` on both `capture_compose` and `capture_containers`,
  frozen on `ComposeTarget` and `ContainerTarget`. Check matching documentation
  for cause-free `Failure(reason="exited")`, completion precedence, and restart
  budgets; the default doesn't make exited long-running members fail fast.
- `fail_on_unhealthy: bool = False` on both captures and frozen targets, plus
  `unhealthy` in `Failure.reason` and reconciliation's `ConvergenceSummary`.
  Non-bools reject before launch. Only eligible running, unpaused,
  non-restarting members within budget fail; completion ignores health. Check
  matching documentation for wire/completion/restart/exit precedence and the
  missing-Health rule, not just the new keyword.
- `capture_containers(client, references, *, completion=(), max_restarts=None,
  fail_on_exit=False, fail_on_unhealthy=False, policy, timeout, cancel=None)` and `ContainerTarget` exported from
  `pywhales.convergence`; the target has `container_ids`, `completion`, and
  `max_restarts` plus both failure flags. The convergence `wait` annotation must accept that target.
  This is separate from the exit-code `docker.container.wait` API.
- Keyword-only `telemetry_observer` on `AdmissionOwner`, plus
  `AdmissionDecisionRecord`, `AdmissionId`, and `AdmissionOutcome` exports from
  `pywhales.telemetry`. Inspect the record fields and `TelemetryRecord` union;
  schema-8 records include `owner_pending_demand_full` in the extended admission
  outcome vocabulary. Launch and refusal records have `admission_id`, while completion records join by
  `attempt_id`. `RefusalRecord` has `phase="refusal"` and only `cancelled`,
  `timeout`, or `limit` outcomes, without duration, byte counts or return code.
  Configure both owner and client observers for correlation, not
  just the client's launch observer. See [Telemetry and admission](telemetry-and-admission.md#measure-admission-waits-and-correlate-launches).
- A keyword-only `timeout: float | None = None` on Compose `build` and `execute`,
  and on `container.wait`, whose overloads return `int` for one reference and
  `list[int]` for an iterable.
- Keyword-only `timeout: float | None = None` on container, image, network,
  volume and system `prune`. Image returns `str`; other blocking prunes return
  `None`, and container `stream_logs=True` returns owned `ProcessStream`.
- `capture_nodes(client, node_ids, *, manager_ids=(), require_leader=False, policy, timeout, cancel=None)`
  and frozen `NodeTarget` exported from `pywhales.convergence`, accepted by `wait`.
  Confirm the node blocker fields, including `leader_mismatch_managers`, on `DeadlineDetail` and matching installed
  help: fixed exact IDs, ready/active nodes, reachable designated managers,
  no version-index fence and a second complete all-clear pass. Enabled leadership
  needs an explicit nonempty manager subset and exactly one observed leader on
  each clear pass, allowing the identity to change; it is not a global election guarantee.
- `replicated_job` and `global_job` in the `ServiceTarget.mode` annotation;
  `help(ServiceTarget)` shows it too. `capture_service` has no job-specific
  parameter, so its signature and the version string can't establish Swarm job
  support. Check the target annotation and the installed package's matching
  documentation; annotations alone don't prove runtime behavior.
- `health_start_interval: int | timedelta | None = None` on container `create`
  and the `run` implementation, also reached by root aliases. Values must be
  positive exact integers or integral-second timedeltas. Run overloads may name
  the typed `RunArgs` keyword bundle instead. Qualify CLI/engine API 1.44+ support
  separately; PyWhales does no version probe or fallback.
- `CreateRequest.health_start_interval: int | None = None`,
  `SecurityProfile.max_health_start_interval: int | None = None`, and operator
  `RuntimePolicy.supported_controls` from `pywhales.profiles`. Profiles accept
  positive exact integer seconds only, require explicit ceiling approval and
  the conditional `health_start_interval` runtime control, and keep `CONTROLS`
  unchanged. See [Startup health intervals and profiles](containers-and-output.md#startup-health-interval-and-approved-profiles).

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
`strict_env_transport`, `strict_workload_errors`, or `inherit_environment` follows the same signature-first rule: confirm it in
`signature(DockerClient)` before assuming it is supported. Opt-in admission
(`pywhales.admission`), bounded observation (the root `observation`,
`capture_inventory`, and `InspectionPolicy` exports, plus `bulk_reload` from
`pywhales.client_config`), and convergence (`pywhales.convergence`, covering
Swarm nodes/services/jobs/stacks, Compose projects, and standalone containers) are separate imports that don't
change default `DockerClient` behavior; verify their presence the same way
before building on them. `pywhales.reconciliation`, `pywhales.profiles`, and
`pywhales.storage` are likewise opt-in imports that the package root doesn't
re-export.

If `signature()` raises `NameError` on an annotation that is imported only for
type checking, as it does for `bulk_reload`'s `InspectionPolicy`, pass
`annotation_format=annotationlib.Format.FORWARDREF` (or `Format.STRING`) after
`import annotationlib`.
