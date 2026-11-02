import json
import sys
from copy import copy, deepcopy
from dataclasses import asdict
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._observation import observation
from pywhales.client_config import ClientConfig
from pywhales.exceptions import (
    DockerTimeout,
    NoSuchContainer,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import (
    CompletionRecord,
    LaunchRecord,
    RefusalRecord,
    TelemetryRecord,
)
from pywhales.telemetry._lifecycle import ExecutionTelemetry
from pywhales.utils import run


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("timeout", [None, 2.0])
def test_metadata_is_sanitized_when_captured_results_contain_secrets(
    raw: bool, timeout: float | None
) -> None:
    records: list[TelemetryRecord] = []
    secret = "telemetry-private-sentinel"
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import os,sys; os.write(1,sys.argv[1].encode()+b'\\xff\\n'); "
            "os.write(2,os.environ['PRIVATE'].encode())",
            secret,
        ],
        host=secret,
        raw_diagnostics=raw,
        telemetry_observer=records.append,
    )

    result = run(
        client.docker_cmd, env={"PRIVATE": secret}, return_stderr=True, timeout=timeout
    )

    assert result == (secret + "\ufffd", secret)
    launch = records[0]
    assert isinstance(launch, LaunchRecord)
    assert launch.family == "captured" and launch.schema_version == 7
    assert launch.admission_id is None
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.attempt_id == launch.attempt_id
    assert completed.schema_version == 7 and completed.return_code == 0
    assert completed.stdin_bytes is None
    assert completed.outcome == "success"
    assert (completed.stdout_bytes, completed.stderr_bytes) == (
        len(secret) + 2,
        len(secret),
    )
    assert completed.duration_seconds >= 0
    assert len(records) == 2
    assert secret not in json.dumps([asdict(record) for record in records])


@pytest.mark.parametrize("failure", [False, True])
def test_original_result_is_preserved_when_observer_throws(failure: bool) -> None:
    records: list[TelemetryRecord] = []

    def throwing(record: TelemetryRecord) -> None:
        records.append(record)
        raise RuntimeError("observer-private-sentinel")

    config = ClientConfig(client_call=[sys.executable], telemetry_observer=throwing)
    code = "import os; os.write(1,b'out\\xff\\n'); os.write(2,b'No such container: private'); "
    code += "raise SystemExit(7)" if failure else "pass"

    if failure:
        with pytest.raises(NoSuchContainer) as raised:
            run([*config.docker_cmd, "-c", code])
        assert raised.value.return_code == 7
        assert raised.value.stdout == "out\ufffd\n"
        assert raised.value.stderr == "No such container: private"
    else:
        assert run([*config.docker_cmd, "-c", code]) == "out\ufffd"
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.outcome == ("failure" if failure else "success")
    assert isinstance(records[0], LaunchRecord)
    assert completed.attempt_id == records[0].attempt_id
    assert completed.return_code == (7 if failure else 0)


def test_timeout_counts_raw_bytes_when_partial_output_is_captured() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )

    with pytest.raises(DockerTimeout) as raised:
        run(
            [
                *config.docker_cmd,
                "-c",
                "import os,threading; os.write(1,b'partial\\xff'); threading.Event().wait()",
            ],
            timeout=0.5,
        )

    assert raised.value.stdout == "partial\ufffd"
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.return_code == raised.value.return_code
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "timeout",
        8,
        0,
    )


def test_counts_are_unavailable_when_output_is_inherited() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )

    assert run([*config.docker_cmd, "-c", "pass"], capture_stdout=False) == ""

    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.return_code == 0
    assert (completed.stdout_bytes, completed.stderr_bytes) == (None, 0)


def test_spawn_failure_preserves_exception_when_observer_throws() -> None:
    records: list[TelemetryRecord] = []
    original = FileNotFoundError("private-path")

    def throwing(record: TelemetryRecord) -> None:
        records.append(record)
        raise ValueError("callback-private")

    config = ClientConfig(client_call=[sys.executable], telemetry_observer=throwing)
    with (
        patch("subprocess.run", side_effect=original),
        pytest.raises(FileNotFoundError) as raised,
    ):
        run(config.docker_cmd)

    assert raised.value is original
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.return_code is None
    assert (completed.outcome, completed.stdout_bytes, completed.stderr_bytes) == (
        "failure",
        None,
        None,
    )


@pytest.mark.parametrize("composition", ["copy", "deepcopy", "list"])
def test_observer_is_snapshotted_at_launch_after_command_copy(composition: str) -> None:
    first: list[TelemetryRecord] = []
    second: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable, "-c", "pass"], telemetry_observer=first.append
    )
    command = {
        "copy": copy(config.docker_cmd),
        "deepcopy": deepcopy(config.docker_cmd),
        "list": list(config.docker_cmd),
    }[composition]
    config.telemetry_observer = second.append

    assert run(command) == ""

    assert first == []
    assert len(second) == 2


def test_supplied_config_wins_over_constructor_observer() -> None:
    selected: list[TelemetryRecord] = []
    ignored: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable, "-c", "pass"], telemetry_observer=selected.append
    )
    client = DockerClient(client_config=config, telemetry_observer=ignored.append)

    assert run(client.docker_cmd) == ""

    assert len(selected) == 2
    assert ignored == []


def test_observation_preflight_emits_only_refusal() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(max_references=1, max_argv_bytes=1, max_response_bytes=8)

    with observation(policy), pytest.raises(ObservationLimitError):
        run([*config.docker_cmd, "-c", "pass"])

    (refusal,) = records
    assert isinstance(refusal, RefusalRecord)
    assert (refusal.family, refusal.outcome) == ("captured", "limit")


def test_bounded_capture_counts_bytes_when_response_limit_fails() -> None:
    records: list[TelemetryRecord] = []
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=8
    )

    with observation(policy), pytest.raises(ObservationLimitError):
        run([*config.docker_cmd, "-c", "import os; os.write(1,b'123456789')"])

    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stdout_bytes) == ("limit", 9)


def test_duration_excludes_admission_and_readiness_clocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pywhales.telemetry import _lifecycle

    records: list[TelemetryRecord] = []
    clock = iter([100.0, 102.5])
    monkeypatch.setattr(_lifecycle, "monotonic", lambda: next(clock))
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )

    assert run([*config.docker_cmd, "-c", "pass"]) == ""

    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.duration_seconds == 2.5


def test_disabled_observer_uses_neither_identifier_nor_telemetry_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pywhales.telemetry import _lifecycle

    def forbidden() -> None:
        raise AssertionError("disabled telemetry performed work")

    monkeypatch.setattr(_lifecycle, "uuid4", forbidden)
    monkeypatch.setattr(_lifecycle, "monotonic", forbidden)
    config = ClientConfig(client_call=[sys.executable, "-c", "pass"])
    assert run(config.docker_cmd) == ""


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (None, "success"),
        (ObservationCancelled(), "cancelled"),
        (ObservationLimitError("total_stream", 1, 2), "limit"),
        (ObservationLimitError("native_argv", None, None), "limit"),
        (ObservationTimeout(), "timeout"),
        (DockerTimeout(["docker"], 1, None, None), "timeout"),
        (KeyboardInterrupt(), "failure"),
        (BufferError(), "failure"),
        (OSError(), "failure"),
        (GeneratorExit(), "closed"),
    ],
)
def test_completion_outcome_follows_the_exception_that_ended_the_launch(
    error: BaseException | None, outcome: str
) -> None:
    records: list[TelemetryRecord] = []

    try:
        with ExecutionTelemetry(records.append, "captured") as telemetry:
            telemetry.start()
            if error is not None:
                raise error
    except BaseException as raised:
        assert raised is error

    launch, completed = records
    assert isinstance(launch, LaunchRecord)
    assert isinstance(completed, CompletionRecord)
    assert launch.schema_version == completed.schema_version == 7
    assert completed.outcome == outcome
