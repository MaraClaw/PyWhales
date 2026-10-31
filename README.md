# PyWhales

[![Run tests](https://github.com/MaraClaw/PyWhales/actions/workflows/python-package.yml/badge.svg)](https://github.com/MaraClaw/PyWhales/actions/workflows/python-package.yml)
[![PyPI](https://img.shields.io/pypi/v/pywhales.svg)](https://pypi.org/project/pywhales/#description)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-Ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Types: ty](https://img.shields.io/badge/types-ty-261230.svg)](https://docs.astral.sh/ty/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/MaraClaw/PyWhales/blob/HEAD/LICENSE)

PyWhales is a typed Python wrapper around the external Docker CLI. It builds `docker` command lines, runs them as subprocesses, and returns the results as Python objects. It isn't an HTTP SDK and doesn't call the Docker Engine API.

* [Documentation](https://github.com/MaraClaw/PyWhales/tree/HEAD/docs)
* [GitHub repository](https://github.com/MaraClaw/PyWhales)

## Requirements

* Python 3.14.10 or newer, on Linux, macOS or Windows.
* The Docker CLI, installed separately. See [the Docker CLI guide](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/docker_client.md#the-docker-cli).
* A reachable Docker daemon for commands that talk to it.
* The Compose and Buildx plugins for the commands that use them (`docker.compose`, `docker.build`).

Importing PyWhales or constructing a client doesn't check that the CLI, daemon or plugins are available. Problems show up when a command runs.

## Install

```bash
uv pip install pywhales
```

## Quickstart

```python
from pywhales import docker

print(docker.run("hello-world", remove=True))
containers = docker.ps()
images = docker.image.list()
```

Building and Compose need local files: a Dockerfile for `build` (`DockerClient.build` is an alias for Buildx) and a Compose file for `compose`.

```python
image = docker.build(".", tags="my-app:latest", load=True)
docker.compose.up(detach=True)
```

## Common tasks

These examples use `alpine`, publish no ports, and clean up after themselves. The first run pulls the image if it isn't local. Commands are argv lists, not shell strings: a string raises `TypeError`.

### Run a detached container safely

```python
from pywhales import docker

with docker.run("alpine", ["sleep", "300"], detach=True, stop_timeout=1) as container:
    state = container.state
    if state is not None:
        print(state.running)  # True
```

`detach=True` returns a `Container` handle instead of output. Leaving the `with` block, normally or through an exception, stops the container if it's running and removes it along with its anonymous volumes. A container that's already gone isn't an error. `stop_timeout=1` caps the shutdown grace period at one second, so cleanup doesn't wait on `sleep`. Nothing is published to the host unless you pass `publish`.

### Execute a command

```python
from pywhales import docker

with docker.run("alpine", ["sleep", "300"], detach=True, stop_timeout=1) as container:
    print(container.execute(["echo", "hello"]))  # hello
    print(container.execute(["pwd"], workdir="/tmp"))  # /tmp
```

`execute()` returns the command's stdout as a `str` with one trailing newline removed. Stderr isn't returned, but a failed command keeps it on the exception. `docker.execute(container, [...])` does the same and also accepts a container name.

### Stream output and close it

```python
from pywhales import docker

with docker.run("alpine", ["sleep", "300"], detach=True, stop_timeout=1) as container:
    with container.execute(
        ["sh", "-c", "echo out; echo err >&2"], stream=True
    ) as output:
        for source, payload in output:
            print(source, payload.decode(errors="replace"), end="")

# stdout out
# stderr err
```

With `stream=True` you get an iterator of `(source, payload)` pairs. `source` is `"stdout"` or `"stderr"`, and `payload` is `bytes`: one line per record, newline included, so decode it yourself. The two sources can arrive in either order. The non-streaming call returns a decoded `str` instead.

The `with` block closes the local `docker` process on every exit path, including `break` and exceptions. A bare `break` doesn't close the stream. Closing affects only the local CLI: a command still running in the container isn't stopped, and the outer block removes the container.

### Catch CLI failures

```python
from pywhales import DockerException, docker
from pywhales.exceptions import DockerTimeout

with docker.run("alpine", ["sleep", "300"], detach=True, stop_timeout=1) as container:
    try:
        container.execute(["sh", "-c", "exit 3"], timeout=10)
    except DockerTimeout:
        print("The local docker CLI exceeded 10 seconds")
    except DockerException as error:
        print(f"{type(error).__name__}: exit code {error.return_code}")
        # DockerException: exit code 3
```

A failing CLI call raises `DockerException`. Subclasses such as `NoSuchContainer` and `DockerTimeout` derive from it, so list them first. `return_code` is the exit code of the local `docker` process, which `docker exec` takes from the command itself. A stream raises the same exceptions when iteration reaches the end of a failed command.

The exception message shows the exit code but withholds the command and output. `docker_command`, `stdout` and `stderr` hold raw values that can include secrets, so log them only on purpose. `timeout` bounds only the local CLI. On expiry PyWhales kills and reaps it, but the command in the container may keep running. See [Exceptions](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/user_guide/exceptions.md) for the full list.

## Custom configuration

`docker` is a shared default client. Create your own `DockerClient` to change its settings:

```python
from pywhales import DockerClient

client = DockerClient(
    context="my-context",
    compose_files=["compose.yaml"],
    default_timeout=30,
)
```

The Docker context must already exist. `default_timeout` is in seconds and bounds each local CLI launch. Expiry kills and reaps the local CLI process, but doesn't roll back work the daemon has already started.

## Good to know

* Containers, images, volumes and other resources are typed handles. Reading a property may inspect the resource and cache the result briefly. Call `reload()` when you want a fresh copy.
* Streaming calls that own a process return iterators. Close them, or use them as context managers, if you stop reading early.
* `PYWHALES_DEBUG=1` turns on execution diagnostics that withhold command arguments, environment values and captured output. `DockerClient(raw_diagnostics=True)` opts a single client into raw diagnostics, so use it only where its logs are safe to expose.
* That redaction covers diagnostics only. Returned data, exception attributes and output PyWhales doesn't capture are not redacted.

## More documentation

* [Docker client and CLI setup](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/docker_client.md)
* [`docker.run()` guide](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/user_guide/docker_run.md)
* [Exceptions](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/user_guide/exceptions.md)
* [Telemetry and diagnostics](https://github.com/MaraClaw/PyWhales/blob/HEAD/docs/template/user_guide/telemetry.md)
* [All user guides](https://github.com/MaraClaw/PyWhales/tree/HEAD/docs/template/user_guide)

## License

PyWhales is released under the MIT license. See [LICENSE](https://github.com/MaraClaw/PyWhales/blob/HEAD/LICENSE).
