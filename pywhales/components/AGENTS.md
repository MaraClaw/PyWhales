# Command Groups

## Local Navigation

The 18 groups are `buildx`, `compose`, `config`, `container`, `context`, `image`,
`manifest`, `network`, `node`, `plugin`, `secret`, `service`, `stack`, `swarm`,
`system`, `task`, `trust`, and `volume`.

- Each group's `cli_wrapper.py` holds its CLI class and resource wrappers where present.
  Its `models.py`, when present, defines typed Docker wire data.
- Empty `__init__.py` files aren't public API entry points.
- Child guides: `buildx/AGENTS.md` (also imagetools), `compose/AGENTS.md`, `container/AGENTS.md`,
  `image/AGENTS.md`, `manifest/AGENTS.md`, and `service/AGENTS.md`.

## Wrapper Changes

- Extend existing `Command` and configured prefixes; reuse shared argument helpers.
- Preserve resource-or-string `Valid*` aliases and shared conversion, not ad hoc name/ID extraction.
- Forward modern options directly; no version probes or fallbacks that silently drop options.
- Model changes need wire fixtures and static contracts; preserve existing wire aliases.
- Container, Compose and service `envs` forwarding follows the
  [runtime environment transport contract](../AGENTS.md#container-environment-transport); keep it centralized.

## Shared Contracts

- Use `_cleanup.py::resource_cleanup` in resource `__exit__` methods. Cleanup `BaseException` plus a body failure
  form a `BaseExceptionGroup`; don't swallow either failure or narrow the catch.
  Container, Image, Volume, Network, Config, Secret, Plugin, and Service nest a `try` inside it that
  returns on only their own `NoSuch*` error; every other removal failure still reaches `resource_cleanup`.
  Context, Builder, and ManifestList use it without that `try`.
  Config, Secret, Plugin, Context, and Service `exists()` returns `False` only for that own error; other failures propagate.
- Eleven scalar JSON hooks retain module-local `run()` and `-> dict[str, JsonValue]`, calling `_parse_single_inspect_json()`.
  Singleton shape and reload identity rules live in [the runtime guide](../AGENTS.md#configuration-and-policy); Builder and ManifestList keep distinct response shapes.
- Config, Node, Secret, and Service inspect use `_swarm_inspect.py::parse_swarm_inspect_results`.
  Resolve reordered responses by exact ID, then exact name (hostname for nodes), then
  unique ID prefix. Validate identities, ambiguity, snapshot consistency, and requested
  multiplicity before associating any snapshot with a resource cache.
- Context, Plugin, and Task list-valued `inspect()` each use one plural CLI call and
  `pywhales/_inspect_validation.py::parse_namespace_inspect_results`. Validate JSON shape, count, models,
  identities, and duplicate consistency for the whole response before hydrating any returned handle.
- Context names must match exactly. Full plugin IDs (64 lowercase hex characters) and task IDs
  (25 lowercase alphanumeric characters) must match exactly; other aliases retain positional association.
  Preserve request order and duplicate handles; `[]` returns without executable lookup. Don't apply these batch positional rules to scalar inspection or Swarm reordering.
- `top.py` shares table parsing, but Docker top takes `ps_args` and Compose top selects
  services with grouped-container or flat-service output. Keep those grammars separate.
  Parsed tables support scalar columns and at most one final free-text field.
  Unsupported layouts raise `TopParseError`; use explicit `raw=True` instead of guessing.

## Local Deadlines

- A keyword-only `timeout` is positive finite seconds for the selected local CLI launch, or `None` to
  inherit `ClientConfig.default_timeout`; wrappers call `utils.validate_timeout` first. Expiry, reaping,
  and stream timing follow [the runtime guide](../AGENTS.md). No deadline undoes daemon or registry work.
- Container and Compose launch/transfer/subscription rules live in their child guides.
  Image transfers/archives and Buildx `build`/`bake`/`prune` also take `timeout`; read wrapper signatures.
- `service.logs()` and `system.events()` take keyword-only `timeout`. Service inspect preflight is outside
  the deadline; logs keep collected text or owned byte streams. Events keep `MappedProcessStream[DockerEvent]`
  (stdout-only JSON mapping, transport ownership). There's no `Service.logs()` handle API.
- `manifest.push()` and `plugin.push()` budget only the push CLI; the manifest-list inspect preflight is
  outside it. `volume.copy()`/`clone()` apply `timeout` to each helper launch, not as one total.
- Swarm and plugin calls that wait on the daemon take keyword-only `timeout`, appended after `*` so earlier
  positionals keep their binding: `service.create/update/rollback/scale` (plus `Service.update`/`scale`),
  `swarm.ca`/`join`, `stack.deploy`, and `plugin.install`/`upgrade` (plus `Plugin.upgrade`). Validation precedes
  executable lookup, env-file reads, policy options, and the `scale` inspect. The scale preflight and the
  post-install plugin inspect are outside the budget; `swarm.ca` budgets its rotate and certificate-read launches
  separately, and an expired first launch skips the read. No call gains `--detach` defaults or version probes.
- Docker option values aren't local deadlines: `plugin.enable(timeout=...)` (HTTP), container `stop_timeout`
  and `health_timeout`, Compose `down`/`restart`/`stop` `timeout`, and `up(wait_timeout=...)`.

## Test Navigation

- Group behavior: `tests/pywhales/components/test_<group>.py`; Buildx has its own subtree. Read [test selection hazards](../../tests/AGENTS.md) before collecting mixed modules.
- Shared top parsing: container/Compose component tests; root aliases: `tests/pywhales/test_docker_client.py`.
- Wire fixtures live in `tests/pywhales/components/jsons/`; static resource and top
  contracts live in `tests/pywhales/test_type_contracts.py`, with group-specific contracts
  alongside it for Buildx and Compose.
- Deadline execution/forwarding (launches, transfers, subscriptions) lives in `components/test_command_deadlines.py`
  and `components/test_deadline_forwarding.py` under `tests/pywhales/`; static shapes are in
  `tests/pywhales/test_command_deadline_types.py`.
