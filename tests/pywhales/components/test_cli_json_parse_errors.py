"""Safe and raw parse failures for captured and streamed CLI JSON, daemon-free.

One-shot surfaces run behind a patched transport. Event surfaces run real
Python children behind a patched stream launcher, so reaping and reader-thread
shutdown are observed rather than assumed.
"""

import json
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales.client_config import ParsingError
from pywhales.components.container.cli_wrapper import ContainerStats
from pywhales.exceptions import DockerException, DockerTimeout, ObservationLimitError
from pywhales.utils import MappedProcessStream, ProcessStream, stream_stdout_and_stderr

SECRET: Final = "parse-diagnostic-sentinel"
DISK_KINDS: Final = ("Images", "Containers", "Local Volumes", "Build Cache")
STATS_ROW: Final = {
    "BlockIO": "1kB / 2kB",
    "CPUPerc": "1.25%",
    "Container": "first",
    "ID": "id-1",
    "MemPerc": "25%",
    "MemUsage": "1MB / 4MB",
    "Name": "first",
    "NetIO": "3kB / 4kB",
}
DEEP_JSON: Final = b"[" * 1_000_000 + SECRET.encode()


def _disk_listing(size: str) -> bytes:
    return "\n".join(
        json.dumps(
            {
                "Type": kind,
                "TotalCount": "1",
                "Active": "0",
                "Size": size,
                "Reclaimable": "1MB (50%)",
            }
        )
        for kind in DISK_KINDS
    ).encode()


@dataclass(frozen=True, slots=True)
class OneShot:
    """A parse surface, a payload it rejects, and the error that rejection is."""

    call: Callable[[DockerClient], object]
    payload: bytes
    cause: type[Exception]
    leaks: bool = False
    source: str | None = None


ONE_SHOT: Final = {
    "disk-free-malformed-json": OneShot(
        lambda c: c.system.disk_free(), b"not-json", json.JSONDecodeError
    ),
    "disk-free-wrong-shape": OneShot(lambda c: c.system.disk_free(), b"[1]", TypeError),
    "disk-free-missing-key": OneShot(
        lambda c: c.system.disk_free(), b'{"Type":"Images"}', KeyError
    ),
    "disk-free-missing-group": OneShot(
        lambda c: c.system.disk_free(),
        _disk_listing("1MB").splitlines()[0],
        KeyError,
    ),
    "disk-free-invalid-model": OneShot(
        lambda c: c.system.disk_free(), _disk_listing(SECRET), ValueError, leaks=True
    ),
    "info-malformed-json": OneShot(
        lambda c: c.system.info(), b"not-json", json.JSONDecodeError
    ),
    "info-wrong-shape": OneShot(lambda c: c.system.info(), b"[]", TypeError),
    "info-invalid-model": OneShot(
        lambda c: c.system.info(),
        json.dumps({"Containers": SECRET}).encode(),
        ValueError,
        leaks=True,
    ),
    "config-malformed-json": OneShot(
        lambda c: c.compose.config(), b"not-json", json.JSONDecodeError
    ),
    "config-wrong-shape": OneShot(lambda c: c.compose.config(), b"[]", TypeError),
    "config-invalid-model": OneShot(
        lambda c: c.compose.config(),
        json.dumps({"services": {"web": {"environment": {"K": [SECRET]}}}}).encode(),
        ValueError,
        leaks=True,
    ),
    "config-json-malformed": OneShot(
        lambda c: c.compose.config(return_json=True), b"{", json.JSONDecodeError
    ),
    "config-json-invalid-model": OneShot(
        lambda c: c.compose.config(return_json=True),
        json.dumps([SECRET]).encode(),
        ValueError,
        leaks=True,
    ),
    "ls-malformed-json": OneShot(
        lambda c: c.compose.ls(), b"not-json", json.JSONDecodeError
    ),
    "ls-wrong-shape": OneShot(lambda c: c.compose.ls(), b"[1]", TypeError),
    "ls-missing-key": OneShot(lambda c: c.compose.ls(), b"[{}]", KeyError),
    "ls-invalid-count": OneShot(
        lambda c: c.compose.ls(),
        json.dumps([{"Name": "p", "Status": f"running(x {SECRET})"}]).encode(),
        ValueError,
        leaks=True,
    ),
    "imagetools-deep-json": OneShot(
        lambda c: c.buildx.imagetools.inspect("example:latest"),
        DEEP_JSON,
        RecursionError,
        source="imagetools manifest",
    ),
    "version-malformed-json": OneShot(
        lambda c: c.version(), SECRET.encode(), json.JSONDecodeError, source="version"
    ),
    "version-deep-json": OneShot(
        lambda c: c.version(), DEEP_JSON, RecursionError, source="version"
    ),
    "stats-malformed-line": OneShot(
        lambda c: c.container.stats(),
        json.dumps(STATS_ROW).encode() + b"\n" + SECRET.encode(),
        json.JSONDecodeError,
        source="container stats",
    ),
    "stats-invalid-record": OneShot(
        lambda c: c.container.stats(),
        json.dumps({**STATS_ROW, "CPUPerc": f"{SECRET}%"}).encode(),
        ValueError,
        leaks=True,
        source="container stats",
    ),
    "stats-deep-json": OneShot(
        lambda c: c.container.stats(),
        DEEP_JSON,
        RecursionError,
        source="container stats",
    ),
}


def _client(*, raw: bool) -> DockerClient:
    client = DockerClient(raw_diagnostics=raw)
    client.client_config._client_call_with_path = ["fake-docker"]
    return client


def _rendered(error: BaseException) -> str:
    return str(error) + repr(error) + "".join(traceback.format_exception(error))


@pytest.mark.parametrize("raw", [False, True], ids=["safe", "raw"])
@pytest.mark.parametrize("case", ONE_SHOT.values(), ids=ONE_SHOT.keys())
def test_one_shot_parse_failure_is_parsing_error_by_policy(
    case: OneShot, raw: bool
) -> None:
    """Given a rejected payload, when parsed, then policy decides what chains."""
    with (
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, case.payload, b""),
        ),
        pytest.raises(ParsingError) as raised,
    ):
        case.call(_client(raw=raw))

    error = raised.value
    if case.source is not None:
        assert str(error) == f"Invalid {case.source} JSON response"
    if raw:
        cause = error.__cause__
        assert isinstance(cause, case.cause)
        if isinstance(cause, json.JSONDecodeError) and SECRET.encode() in case.payload:
            assert cause.doc == case.payload.decode().splitlines()[-1]
        assert (SECRET in _rendered(error)) is case.leaks
    else:
        assert error.__cause__ is None
        assert error.__context__ is None
        assert error.__suppress_context__ is True
        assert SECRET not in _rendered(error)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.system.disk_free(),
        lambda c: c.system.info(),
        lambda c: c.compose.config(),
        lambda c: c.compose.ls(),
    ],
    ids=["disk-free", "info", "config", "ls"],
)
def test_command_failure_is_not_a_parsing_error(
    call: Callable[[DockerClient], object],
) -> None:
    """Given a failing CLI, when called, then the launch error is untouched."""
    failed = subprocess.CompletedProcess([], 1, b"", b"boom")
    with patch("subprocess.run", return_value=failed), pytest.raises(DockerException):
        call(_client(raw=False))


def test_stats_preserves_typed_records_in_cli_order() -> None:
    second = {**STATS_ROW, "ID": "id-2", "Name": "second", "CPUPerc": "0.5%"}
    payload = "\n".join(map(json.dumps, (STATS_ROW, second))).encode()
    done = subprocess.CompletedProcess([], 0, payload, b"")
    with patch("subprocess.run", return_value=done):
        result = _client(raw=False).container.stats()

    assert all(isinstance(record, ContainerStats) for record in result)
    assert [(r.container_id, r.container_name, r.cpu_percentage) for r in result] == [
        ("id-1", "first", 1.25),
        ("id-2", "second", 0.5),
    ]
    assert [r.memory_used for r in result] == [1_000_000, 1_000_000]


@pytest.mark.parametrize(
    ("target", "call", "launch_count"),
    [
        ("pywhales.docker_client.run", lambda c: c.version(), 1),
        (
            "pywhales.components.container.cli_wrapper.run",
            lambda c: c.container.stats(),
            1,
        ),
        (
            "pywhales.components.buildx.cli_wrapper.run",
            lambda c: c.buildx.bake(print=True),
            1,
        ),
        ("pywhales.components.buildx.cli_wrapper.run", lambda c: c.buildx.bake(), 2),
    ],
    ids=["version", "stats", "bake-print", "bake-follow-up"],
)
@pytest.mark.parametrize(
    "error",
    [
        DockerException(["fake-docker"], 1),
        DockerTimeout(["fake-docker"], 1),
        ObservationLimitError("total_io", 1, 2),
        RecursionError("outside the parser"),
    ],
    ids=["cli-failure", "timeout", "observation-limit", "outside-recursion"],
)
def test_new_parse_surfaces_propagate_launch_errors(
    target: str,
    call: Callable[[DockerClient], object],
    launch_count: int,
    error: Exception,
) -> None:
    """Launch errors stay unchanged, even when their type is a parse error type."""
    with (
        patch(target, side_effect=[""] * (launch_count - 1) + [error]),
        pytest.raises(type(error)) as raised,
    ):
        call(_client(raw=False))
    assert raised.value is error


def test_raw_compose_config_preserves_large_integer_and_unicode() -> None:
    payload = (
        '{"large": 1208925819614629174706176, '
        '"unicode": "雪🐳", "surrogate": "\\ud800"}'
    ).encode()
    done = subprocess.CompletedProcess([], 0, payload, b"")
    with patch("subprocess.run", return_value=done):
        result = _client(raw=False).compose.config(return_json=True)

    assert type(result["large"]) is int
    assert result["large"] == 2**80
    assert result["unicode"] == "雪🐳"
    assert result["surrogate"] == "\ud800"


SYSTEM_GOOD: Final = '{"Type":"container","Action":"start"}'
COMPOSE_GOOD: Final = '{"type":"container","action":"start","service":"web"}'
EVENT_SOURCES: Final = {
    "system": (
        "pywhales.components.system.cli_wrapper.stream_stdout_and_stderr",
        lambda c: c.system.events(),
        SYSTEM_GOOD,
    ),
    "compose": (
        "pywhales.components.compose.cli_wrapper.stream_stdout_and_stderr",
        lambda c: c.compose.events(),
        COMPOSE_GOOD,
    ),
}
BAD_RECORDS: Final = {
    "malformed": f"{SECRET}-not-json",
    "wrong-shape": f'["{SECRET}"]',
    "invalid-model": json.dumps({"time": SECRET}),
}
CHILD: Final = (
    "import sys\nfor record in sys.argv[1:]:\n    print(record, flush=True)\ninput()\n"
)


def _event_child(*records: str) -> list[str]:
    return [sys.executable, "-c", CHILD, *records]


def _assert_reaped(pipe: ProcessStream) -> None:
    assert pipe._process.poll() is not None
    assert all(not thread.is_alive() for thread in pipe._threads)


@pytest.mark.parametrize("raw", [False, True], ids=["safe", "raw"])
@pytest.mark.parametrize("bad", BAD_RECORDS.values(), ids=BAD_RECORDS.keys())
@pytest.mark.parametrize("source", EVENT_SOURCES)
def test_events_raise_parsing_error_once_and_reap_child(
    source: str, bad: str, raw: bool
) -> None:
    """Given a bad record, when reached, then good records came first and the
    error is raised once after the child is reaped.
    """
    target, start, good = EVENT_SOURCES[source]
    with (
        stream_stdout_and_stderr(
            _event_child(good, bad, good), pipe_stdin=True
        ) as pipe,
        patch(target, return_value=pipe),
    ):
        stream = start(_client(raw=raw))
        assert isinstance(stream, MappedProcessStream)

        assert next(stream) is not None
        with pytest.raises(ParsingError) as raised:
            next(stream)

        _assert_reaped(pipe)
        if raw:
            assert raised.value.__cause__ is not None
        else:
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None
            assert raised.value.__suppress_context__ is True
            assert SECRET not in _rendered(raised.value)
        with pytest.raises(StopIteration):
            next(stream)
        assert list(stream) == []


@pytest.mark.parametrize("launched_raw", [False, True], ids=["safe", "raw"])
@pytest.mark.parametrize("source", EVENT_SOURCES)
def test_events_snapshot_diagnostics_flag_at_launch(
    source: str, launched_raw: bool
) -> None:
    """Given a flag flipped after launch, then the launch-time value applies."""
    target, start, good = EVENT_SOURCES[source]
    client = _client(raw=launched_raw)
    with (
        stream_stdout_and_stderr(
            _event_child(good, BAD_RECORDS["invalid-model"]), pipe_stdin=True
        ) as pipe,
        patch(target, return_value=pipe),
    ):
        stream = start(client)
        client.client_config.raw_diagnostics = not launched_raw

        next(stream)
        with pytest.raises(ParsingError) as raised:
            next(stream)

    assert (raised.value.__cause__ is not None) is launched_raw
    assert (SECRET in _rendered(raised.value)) is launched_raw


@pytest.mark.parametrize("source", EVENT_SOURCES)
def test_events_process_failure_is_not_a_parsing_error(source: str) -> None:
    """Given a child that fails after a good record, then the stream error wins."""
    target, start, good = EVENT_SOURCES[source]
    script = f"import sys; print({good!r}, flush=True); sys.exit(3)"
    with (
        stream_stdout_and_stderr([sys.executable, "-c", script]) as pipe,
        patch(target, return_value=pipe),
    ):
        stream = start(_client(raw=False))

        assert next(stream) is not None
        with pytest.raises(DockerException):
            next(stream)
