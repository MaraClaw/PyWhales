# Capturing exceptions

## Exception classes

CLI failures raise `DockerException`, or a child class for more specific errors.

Those are the child classes:

* `NoSuchContainer`
* `NoSuchImage`
* `NoSuchService`
* `NotASwarmManager`
* `NoSuchVolume`
* `NoSuchConfig`
* `NoSuchSecret`
* `NoSuchPlugin`
* `NoSuchContext`
* `GpuUnavailable`, when the daemon rejects a `--gpus` or CDI device request

`GpuSupportError`, raised by the opt-in `require_gpu=True` preflights of container
`run()` and `create()`, Swarm service `create()` and Compose `up()` and `run()`, is
not a `DockerException`: no CLI command failed. Its `reason` values are listed in
[GPUs, vGPU and CDI devices](gpus.md#checking-before-a-run).

A class is chosen from the text docker wrote to stderr. A line has to begin with the
resource kind's own phrase, such as `No such secret: x` or `config x not found`,
optionally after `Error response from daemon: ` or `Error: `. The name in the
message and file paths never decide the class, so a secret called `network-x` is
still a `NoSuchSecret`. Output of your own application that `run` or `exec`
forwards on stderr can begin a line with one of those phrases and be misclassified;
PyWhales does nothing to prevent that.

Config, secret, plugin, context, and service handles have an `exists()` method that
returns `False` only for their own `NoSuch...` class. Other failures, including
`NotASwarmManager`, propagate. A config, secret, plugin, or service that was removed
inside its `with` block is not an error when the block exits.

Docker exceptions have these 4 attributes:

* docker_command: the docker command used internally, as a list of strings.
* return_code: the exit code docker client exited with, as an int
* stdout: the content that docker wrote to stdout, as a string, or `None`
* stderr: the content that docker wrote to stderr, as a string, or `None`

Automatic failure messages, debug printing, and logger records withhold command,
environment, and captured-output values by default. Exception classes and exit
codes remain available. The four attributes above still contain raw values;
avoid logging them where they could expose credentials or application data.

Use `DockerClient(raw_diagnostics=True)` on a separate troubleshooting client to
enable detailed diagnostics. This also permits raw inspect-response dumps when
model parsing fails. Returned payloads, uncaptured terminal output, and values
you log yourself are not protected by this policy.

## Parsing errors

Import `ParsingError` from `pywhales.client_config`. Inspect parsing and the
Buildx builder reload/list, Compose config/ls/events, system disk-free/events/info,
and imagetools manifest parsers use it for malformed CLI responses. It is separate
from `DockerException` and is not a `ValueError` subclass. Safe mode suppresses
payload-bearing parsing chains; `raw_diagnostics=True` chains the original error
for these command parsers. Event streams retain the launch-time diagnostics
setting and close the local CLI before delivering a malformed-record error.
`DockerClient.version()` and container stats retain their existing parsing errors.

## Example

```python
import logging
from pywhales import DockerClient
from pywhales.exceptions import DockerException

client = DockerClient(compose_files=["/tmp/docker-compose.yml"])
try:
    client.execute("my-service", ["arg1", "arg2"])
except DockerException as e:
    print(f"Docker command failed with exit code {e.return_code}")
```
