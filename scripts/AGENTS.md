# Scripts Guide

Contributor tooling, not installed APIs: `pyproject.toml` packages only `pywhales*`, and `scripts/` has no `__init__.py`.
Run its packages from the repository root as `python -m scripts.<package>`. The root `AGENTS.md` owns the shared safety
rules (daemon provisioning, the destructive suite, release steps) and the repo-wide Ruff and ty setup; this guide maps
what lives here.

## Local Map

| Path | Role |
| --- | --- |
| `ci-setup.sh`, `add-local-docker-registry.sh`, `download-docker-plugins.sh`, `docker-plugin-checksums.txt` | CI provisioning and plugin pins |
| `install-uv.sh` | Pipes the remote uv installer into `sh` |
| `benchmark_docker_overhead.py` | Simulated wrapper-overhead profiler |
| `fleet_contract/` | Contract editing map in `fleet_contract/AGENTS.md`; commands and semantics in its `README.md` |
| `fleet_harness/` | Simulation, transport decisions, and CLI contracts in `fleet_harness/AGENTS.md` |
| `delivery/` | Tested delivery chain; see `delivery/AGENTS.md` |

## Shell Scripts

- The root guide covers the provisioning hazards. Read these scripts; don't run them on a workstation.
- `python-package.yml` calls `ci-setup.sh` from three jobs (component, other, coverage) and `download-docker-plugins.sh`
  from the amd64 and native ARM64 Compose and Buildx smoke jobs. Read those jobs before editing either script.
- `COMPOSE_VERSION` and `BUILDX_VERSION` default to 5.6.1 and 0.38.1. The script fails before downloading unless the
  checksum file holds exactly one 64-hex pin for that name, version and architecture, and it verifies with `sha256sum -c` in a staging
  directory before `mv`. Keep that order. A bump changes the pin file and the plugin rows in
  `fleet_contract/fixtures/support-matrix.json`.

## Benchmark

- `subprocess.run` becomes a recorder and `subprocess.Popen` raises, so no Docker or child process starts. It times
  Python wrapper work, not builds. `--repetitions N` takes 1 to 1000 (default 20) and prints JSON.
- Documented run from the root: `.venv/bin/python scripts/benchmark_docker_overhead.py`.
- `SimulatedExecution.run` raises `RuntimeError` for any CLI call without a fixture response, so a scenario that adds a
  call needs one there. Test: `tests/pywhales/test_docker_overhead_benchmark.py`.
