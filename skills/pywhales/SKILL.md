---
name: pywhales
description: >-
  Use the installed pywhales package to automate Docker from Python applications
  and scripts. Use when translating docker run/build/compose commands into Python,
  managing containers or images with pywhales, pulling/pushing/saving/loading
  images, using Buildx or Compose from Python, isolating CLI child environments,
  enforcing strict environment-only transport for container envs and build args,
  adding telemetry or admission
  limits, bounding inspection and archive uploads, waiting for Swarm
  service/job/stack, Compose project, or standalone container convergence with
  completion and restart budgets, or
  troubleshooting pywhales usage, output types,
  streaming, and cleanup. This skill is for consuming pywhales, not developing
  or contributing to the library.
compatibility: >-
  Python >=3.14.10 with pywhales installed. Executing Docker operations requires
  the Docker CLI and the selected engine; Compose and Buildx need their plugins.
  Imports, client construction, and API introspection do not require Docker.
---

# PyWhales

Use the Python API that wraps the Docker CLI. Don't substitute Docker SDK APIs
or treat the `pywhales` console command as a Docker command runner.
This guidance describes current source APIs with project version 1.1.2.
An installed version string alone isn't proof of support; check actual
capabilities before use.

Work in the user's application or script with the installed package. A PyWhales
repository checkout, changes to its internals, and its contributor test suite
are not part of this workflow.

## Workflow

1. Identify the target context or host, project, input paths, and desired result.
   Keep existing resources separate from disposable resources the task creates.
2. Read the relevant reference below, not the whole bundle.
3. Check installed signatures, return annotations/overloads, and `help()` before
   adding unfamiliar arguments or assuming a closeable stream. For inventory,
   check supported kinds too; for Swarm waits, check the supported service modes.
   For bounded uploads, isolated environments, and container convergence, check
   the policy fields, client flags, target types, and capture options rather than
   inferring support from the version string.
   See [Usage validation](references/validation.md);
   these checks need no repository checkout or running engine.
4. Translate commands into argument sequences and verified keyword arguments.
   Choose captured output, detached objects, or streaming before writing code.
5. Scope clients instead of changing the user's active Docker context. Give owned
   containers and streams explicit lifetimes; don't attach cleanup to borrowed objects.
   Reload resource handles explicitly when verifying state after a mutation;
   don't use a fixed sleep to expire their inspection cache.
6. Validate the smallest authorized scenario. Report what ran, its output type,
   cleanup, and any unavailable engine or plugin checks.

## Topic Router

| Read when | Reference |
| --- | --- |
| Setting up imports, local/remote clients, CLI prerequisites, isolated child environments, default deadlines, strict environment transport, or inspect/reload errors | [Client, setup, and errors](references/client-and-errors.md) |
| Translating run/exec/ps, passing container environment values, waiting on containers, choosing output modes, mounting files, or cleaning up containers and streams | [Containers, output, and lifecycle](references/containers-and-output.md) |
| Pulling, pushing, saving, or loading images and registry archives | [Registry transfers and archives](references/registry-and-archives.md) |
| Building images, passing build args, selecting exporters, streaming builds, or checking Dockerfiles | [Buildx](references/buildx.md) |
| Selecting Compose files/projects, building services, running jobs, or managing a stack | [Compose](references/compose.md) |
| Observing executions with sanitized telemetry or bounding concurrent work with admission | [Telemetry and admission](references/telemetry-and-admission.md) |
| Bounding inspection, inventory, streams, or stdin uploads; waiting for Swarm services/jobs/stacks, Compose services, or standalone containers with completion and restart budgets; or evaluating opt-in reconciliation | [Observation budgets and convergence](references/observation-and-convergence.md) |
| Checking installed API support and verifying the user's automation | [Usage validation](references/validation.md) |

## Quick Translation

| Docker CLI intent | PyWhales entry point |
| --- | --- |
| `docker run ...` | `docker.run(image, ["arg", ...], ...)` |
| `docker exec ...` | `docker.execute(container, ["arg", ...])`, not `docker.exec` |
| `docker ps` | `docker.ps()` or `docker.container.list()` |
| `docker build ...` | `docker.build(...)`, an alias of `docker.buildx.build(...)` |
| `docker compose ...` | A scoped `DockerClient(...).compose` namespace |

Pass commands as lists, not shell command strings. Use a shell inside the
container only when the task actually requires shell syntax. A detached run
returns a `Container`, not logs; a build doesn't always return a local `Image`.
Read the relevant output contract before consuming either result.
