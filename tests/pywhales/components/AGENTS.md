# Component Test Selection

Paths below are relative to this directory; run commands from the repository root.
The [parent test guide](../../AGENTS.md#runtime-fixtures-and-shared-state) owns runtime fixtures, provisioning, registry, Swarm and pruning rules.
Its [JSON fixture rules](../../AGENTS.md#json-fixture-rules) also apply here.
For wrapper edits, read the production [component](../../../pywhales/components/AGENTS.md),
[Buildx/imagetools](../../../pywhales/components/buildx/AGENTS.md) and [Compose](../../../pywhales/components/compose/AGENTS.md) guides.

## Selection map

| Surface | Start here | Boundary |
| --- | --- | --- |
| Group commands, resources and wire models | `test_<group>.py`, including `test_image.py` | Mixed live, model and mocked cases; classify each test and its fixtures. |
| Compose top, logs, events and projects | `test_compose.py` | Mocked commands, local children and live projects share one module with a collection probe. |
| Buildx and registry manifests | `buildx/test_buildx_cli_wrapper.py`, `buildx/imagetools/test_imagetools_cli_wrapper.py` | Global-client integration, including cases without a runtime fixture. |
| Manifest model snapshots | `buildx/imagetools/test_models.py`, `jsons/` | The model module reads saved JSON without CLI launches; fixture directories aren't test selectors. |
| Environment forwarding across output branches | `test_env_forwarding.py`, `test_env_branch_transport.py` | Fake Python CLIs record argv/environment forwarding versus literal fallback across captured, streamed, detached and TTY branches. |
| Safe/raw JSON failures and network creation | `test_cli_json_parse_errors.py`, `test_network_create.py` | Patched captures and real Python children, not Docker. |
| Launch/transfer/subscription/prune deadlines | `test_command_deadlines.py`, `test_deadline_forwarding.py` | Temporary Python CLI execution versus mocked forwarding; inspect the selected fixture. |
| Modern option and return contracts | `../test_buildx_modernization.py`, `../test_compose_modernization.py` | Simulated commands and local children, distinct from installed-plugin integration. |
| Installed modern Docker capabilities | `test_docker_modern_integration.py` | Explicit `docker` mark; real executable, daemon and pinned Compose/Buildx plugins, even for dry-run/report cases. |
| Output enums, JSON values and static types | `../test_*enum_json*.py`, `../test_*types.py`, `../test_*type_contracts.py` | See the parent's static-contract rules; pytest doesn't execute `_check_*` functions. |

## Collection and shared-state traps

- The automatic `docker` mark follows `docker_client` fixture dependencies, not every CLI call. `-m 'not docker'` isn't a complete daemon-free guarantee.
- `test_compose.py` evaluates `pywhales.docker.compose.is_installed()` while importing, before marker deselection, and creates module-level clients.
- Buildx driver fixtures and imagetools call the global `docker` client; cases without `docker_client` remain unmarked. A patched test body can still enter live builder-selection fixtures.
- Imagetools `dry_run=True` still launches the installed plugin and can use registry sources; it isn't a mock boundary.
- `test_compose_build_with_dependencies` copies inputs to `tmp_path` but uses project `withdeps-test` and deletes/rebuilds fixed tags `withdeps_db` and `withdeps_app`.

## Explicit daemon-free samples

Model-only, with no runtime fixture or availability probe:

```bash
uv run --locked pytest tests/pywhales/components/buildx/imagetools/test_models.py -q
```

Fake-boundary selection, including real Python subprocesses, without collecting the mixed Compose/Buildx modules:

```bash
uv run --locked pytest tests/pywhales/components/test_env_forwarding.py tests/pywhales/components/test_env_branch_transport.py tests/pywhales/components/test_cli_json_parse_errors.py tests/pywhales/components/test_network_create.py -q
```

Real Python subprocesses don't imply daemon access; these samples aren't process-free tests.
All five prune deadline wrappers are covered by those two deadline modules without running Docker prune. Keep container's unconsumed-stream expiry, once-only error delivery, early close and joined cleanup covered alongside blocking partial output, defaults/overrides, validation-before-filter side effects and natural failures. Static signatures/returns live in `../test_command_deadline_types.py` and `../test_type_contracts.py`.
`bake_tests/`, `build_with_dependencies_test/`, `my_service_build/` and adjacent Compose YAML files are project inputs used by tests, not guide locations.
