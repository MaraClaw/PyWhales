import sys
from time import monotonic
from typing import Literal, assert_never
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._observation import observation
from pywhales.exceptions import DockerTimeout, ObservationLimitError, ObservationTimeout
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import CompletionRecord, LaunchRecord, TelemetryRecord
from pywhales.utils import run, stream_stdout_and_stderr


@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
@pytest.mark.parametrize("scope_timeout", [False, True])
def test_real_deadline_projects_the_same_reason_across_execution_families(
    family: Literal["captured", "stream", "archive_load", "archive_save"],
    scope_timeout: bool,
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import threading; threading.Event().wait()",
        ],
        default_timeout=None if scope_timeout else 0.2,
        telemetry_observer=records.append,
    )
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=1024
    )

    with (
        patch.object(client.image, "inspect", return_value=[]),
        pytest.raises(ObservationTimeout if scope_timeout else DockerTimeout),
        observation(policy, deadline=monotonic() + 0.2 if scope_timeout else None),
    ):
        match family:
            case "captured":
                run(client.docker_cmd)
            case "stream":
                with stream_stdout_and_stderr(client.docker_cmd) as stream:
                    list(stream)
            case "archive_load":
                client.image.load(b"input")
            case "archive_save":
                list(client.image.save("sample"))
            case unreachable:
                assert_never(unreachable)

    launch, completed = records
    assert isinstance(launch, LaunchRecord)
    assert isinstance(completed, CompletionRecord)
    assert launch.family == completed.family == family
    assert completed.attempt_id == launch.attempt_id
    assert completed.return_code is not None
    assert (completed.outcome, completed.stop_reason) == (
        "timeout",
        "scope_timeout" if scope_timeout else "command_timeout",
    )


@pytest.mark.parametrize("close_first", [False, True])
def test_stream_keeps_stop_reason_after_terminal_state_is_cleared(
    close_first: bool,
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[
            sys.executable,
            "-c",
            "import os,threading; os.write(1,b'overflow'); threading.Event().wait()",
        ],
        telemetry_observer=records.append,
    )
    policy = InspectionPolicy(
        max_references=1,
        max_argv_bytes=65536,
        max_response_bytes=1024,
        max_total_stream_bytes=4,
    )
    with observation(policy):
        stream = stream_stdout_and_stderr(client.docker_cmd)
    try:
        assert stream._released.wait(3)
        if close_first:
            stream.close()
        with pytest.raises(ObservationLimitError) as failure:
            next(stream)
        stream.close()
        stream.close()

        assert failure.value.reason == "total_stream"
        launch, completed = records
        assert isinstance(launch, LaunchRecord)
        assert isinstance(completed, CompletionRecord)
        assert completed.attempt_id == launch.attempt_id
        assert (completed.outcome, completed.stop_reason) == ("limit", "total_stream")
        assert completed.return_code == stream._process.returncode
        assert list(stream) == []
        assert not stream._watchdog.is_alive()
        assert all(not thread.is_alive() for thread in stream._threads)
    finally:
        stream.close()
