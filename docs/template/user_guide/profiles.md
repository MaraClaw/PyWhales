# Opt-In Security And Resource Profiles

Import policy APIs from `pywhales.profiles`. Nothing enables enforcement on the
shared `docker` client or changes direct `DockerClient.create` calls. A profile
is an application boundary, not daemon authorization or tenant isolation. Anyone
with direct Docker access can bypass it.

## Approve Policy, Then Plan Offline

Every `CreateRequest` field is explicit. The supported surface is deliberately
closed: no extra kwargs, environment/env-file values, devices, raw mount strings,
namespace overrides or label files. Omitted Docker options cannot be supplied
through the profile API. Use exact approved mount references for externally
managed secrets, never put secret contents in commands or policy metadata.

This example only constructs objects and plans; it does not resolve an executable
or contact Docker. Its limits, registry, runtime, network and log policy are
illustrative operator inputs, **not a deployable or recommended security policy**.
Choose approval values independently of the untrusted request.

```python
from dataclasses import replace

from pywhales import DockerClient
from pywhales.admission import EndpointIdentity
from pywhales.profiles import (
    CONTROLS,
    CreateRequest,
    EndpointBinding,
    LogMode,
    LogPolicy,
    Mount,
    PolicyError,
    ResourceLimits,
    RuntimePolicy,
    SecurityProfile,
    plan_create,
)

endpoint = EndpointIdentity("offline")
client = DockerClient(host="unix:///operator-selected/docker.sock")
binding = EndpointBinding(endpoint, client.client_config)
secret_reference = Mount(
    kind="bind",
    source="/operator-managed/token",
    target="/run/secrets/token",
    read_only=True,
)
logging = LogPolicy(
    driver="json-file",
    max_size_bytes=1_000_000,
    max_files=2,
    mode=LogMode.BLOCKING,
    loss_approved=False,
    buffer_bytes=None,
)
profile = SecurityProfile(
    name="example-batch",
    version="v1",
    bindings=(binding,),
    registries=frozenset({"registry.example"}),
    require_digest=True,
    users=frozenset({"1000:1000"}),
    capabilities=frozenset(),
    allow_privileged=False,
    require_read_only=True,
    security_options=("no-new-privileges=true",),
    mounts=frozenset({secret_reference}),
    networks=frozenset({"isolated"}),
    ceilings=ResourceLimits(cpus=1.0, memory_bytes=200_000_000, pids=64),
    logging=logging,
    runtimes=(
        RuntimePolicy(
            name="runc",
            rootless=False,
            supported_controls=CONTROLS,
        ),
    ),
)
request = CreateRequest(
    endpoint=endpoint,
    image="registry.example/team/app@sha256:" + "a" * 64,
    command=("/app",),
    user="1000:1000",
    capabilities=(),
    privileged=False,
    read_only=True,
    security_options=("no-new-privileges=true",),
    mounts=(secret_reference,),
    networks=("isolated",),
    resources=ResourceLimits(cpus=0.5, memory_bytes=100_000_000, pids=32),
    logging=logging,
    runtime="runc",
    rootless=False,
    pull="never",
)
plan = plan_create(profile, request, client)
print(plan.summary)
try:
    plan_create(profile, replace(request, image="registry.example/app:latest"), client)
except PolicyError as error:
    print(error.field, error.reason.value)
```

The summary contains profile name/version, endpoint alias and approval state,
not the image, command, mounts, security options or endpoint credentials. Errors
contain only a field and fixed `PolicyReason`. Keep endpoint aliases and profile
names free of secrets. The plan retains confidential execution inputs in memory;
do not serialize its dataclass fields or use it as an audit dump. Caller logging,
explicit returned output, tracebacks with local variables and raw diagnostics are
outside the sanitized summary boundary.

## Binding And Execution

`EndpointIdentity` is a reporting alias, not verified daemon identity.
`EndpointBinding` records the **exact caller-approved `ClientConfig` object** and
a snapshot of its settings. Planning and execution reject an equal but different
config, a replaced container-group config, or changed config fields before SDK
preflight, including changes to an already populated executable-prefix cache.
Execution alone may resolve an initially empty cache after full validation; the
binding retains that one controlled transition for subsequent checks. External
cache population after binding is not implicitly approved.
The caller must approve and keep executable resolution, Docker environment,
context/config files, credentials and the selected remote endpoint stable. Changes
outside the Python config cannot be detected by this binding. Revalidation is
observational, not an atomic concurrency fence.

After separately authorizing real Docker effects, call
`create_from_plan(client, plan)` from `pywhales.profiles`. It revalidates the whole
request and binding, then delegates to the existing `client.container.create`.
It can inspect or pull according to the explicit `pull` policy before creating;
it does not start the returned container or automatically retry failed creation.
The returned container retains the originating config. Ordinary direct SDK calls
remain permissive and retain their existing defaults.

Capabilities drop `ALL` before adding the approved set. CPU count, memory bytes
and PID count must be positive and within ceilings. Memory-swap is set equal to
memory so this supported profile surface does not request additional swap.
Mounts are exact bind/volume source-target-read-only tuples, not path-prefix
allowlists; duplicate targets and comma/quote/control-character fragments reject.
The root filesystem flag is separate from writable mounts. Security options are
an exact sequence; network choices must be nonempty and approved. Images must
have an explicit registry and a supported repository/tag/SHA-256 reference;
digest-required profiles reject mutable references without substituting an image.

`RuntimePolicy` is the operator's qualified capability declaration, not a probe.
Every control named by `CONTROLS` must be supported for the selected runtime and
rootless setting. An optional `CreateRequest.health_start_interval` is a positive
exact integer in seconds. To approve it, set the independent positive integer
`SecurityProfile.max_health_start_interval` ceiling and add
`"health_start_interval"` to the selected runtime's `supported_controls`.
An equal value is permitted; no ceiling means the override is unapproved. Omitting
the request does not require this additional control. It leaves the image's health
command and start period unchanged. Operators must qualify Docker CLI and daemon
support for API 1.44+; PyWhales does not probe or fall back.

Rootless is not enabled by a Docker create flag: the approved
endpoint must already select that runtime mode. Qualify workloads, network,
cgroups, swap and log behavior on an authorized disposable platform before
asserting effectiveness. No rootless parity or isolation certification is made.

## Logging And Production Qualification

Only rotating `json-file` and `local` drivers are in this surface. Retention sizes
are bytes and file counts. Blocking mode explicitly accepts backpressure;
non-blocking mode requires `loss_approved=True` and positive `buffer_bytes`.
Neither choice guarantees complete audit delivery. These are Docker daemon log
options, not the SDK process-stream buffer budget.

Operators own profile distribution/version approval, endpoint access, runtime
qualification, allowed images, secret references, resource ceilings and audit
loss decisions. Production qualification remains blocked until those decisions
and separately authorized effective-configuration checks are complete. This
implementation's acceptance evidence is offline only.

{{autogenerated}}
