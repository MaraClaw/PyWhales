import json
import sys
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._observation import observation
from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.exceptions import (
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
)
from pywhales.inventory import InspectionPolicy
from pywhales.telemetry import AdmissionDecisionRecord, RefusalRecord, TelemetryRecord
from pywhales.utils import run, stream_stdout_and_stderr


def policy(argv: int = 65536) -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=argv, max_response_bytes=1024
    )


@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "limit"])
def test_preflight_refusal_preserves_exception_and_snapshot(
    family: str, outcome: str
) -> None:
    records: list[TelemetryRecord] = []
    later: list[TelemetryRecord] = []
    consumed: list[bool] = []
    secret = "private-refusal-payload"
    client = DockerClient(
        client_call=[sys.executable],
        host=secret,
        raw_diagnostics=True,
        telemetry_observer=records.append,
    )
    error = {
        "cancelled": ObservationCancelled(),
        "timeout": ObservationTimeout(),
        "limit": ObservationLimitError("argv", 1, 2),
    }[outcome]

    def reject(*args: object) -> None:
        client.client_config.telemetry_observer = later.append
        raise error

    def chunks() -> Iterator[bytes]:
        consumed.append(True)
        yield secret.encode()

    target = (
        "pywhales._bounded_capture.check_scoped_launch"
        if family == "captured"
        else "pywhales._scoped_launch.check_scoped_launch"
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(target, side_effect=reject),
        patch("subprocess.Popen") as spawn,
        patch("pywhales.utils.Popen") as stream_spawn,
        patch("pywhales.components.image.cli_wrapper.Popen") as archive_spawn,
        observation(policy()),
        pytest.raises(type(error)) as raised,
    ):
        if family == "captured":
            run([*client.docker_cmd, secret])
        elif family == "stream":
            stream_stdout_and_stderr([*client.docker_cmd, secret])
        elif family == "archive_load":
            client.image.load(chunks())
        else:
            archive = client.image.save(secret)
            assert records == []
            next(archive)

    assert raised.value is error
    spawn.assert_not_called()
    stream_spawn.assert_not_called()
    archive_spawn.assert_not_called()
    assert consumed == [] and later == []
    (record,) = records
    assert isinstance(record, RefusalRecord)
    assert (record.family, record.outcome, record.schema_version, record.phase) == (
        family,
        outcome,
        6,
        "refusal",
    )
    assert record.admission_id is None and len(record.attempt_id) == 32
    assert secret not in json.dumps(asdict(record))
    with pytest.raises(FrozenInstanceError):
        record.__setattr__("outcome", "limit")


@pytest.mark.parametrize("save", [False, True])
def test_filename_refusal_uses_existing_execution_family(
    save: bool, tmp_path: Path
) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        observation(policy(1)),
        pytest.raises(ObservationLimitError),
    ):
        if save:
            client.image.save("sample", output=tmp_path / "archive")
        else:
            client.image.load(tmp_path / "archive")
    (record,) = records
    assert isinstance(record, RefusalRecord)
    assert (record.family, record.outcome) == (
        "captured" if save else "stream",
        "limit",
    )


@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
def test_disabled_refusal_does_not_generate_identifiers_or_read_telemetry_clock(
    family: str,
) -> None:
    client = DockerClient(client_call=[sys.executable])
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch("pywhales.telemetry._lifecycle.uuid4", side_effect=AssertionError),
        patch("pywhales.telemetry._lifecycle.monotonic", side_effect=AssertionError),
        observation(policy(1)),
        pytest.raises(ObservationLimitError),
    ):
        if family == "captured":
            run(client.docker_cmd)
        elif family == "stream":
            stream_stdout_and_stderr(client.docker_cmd)
        elif family == "archive_load":
            client.image.load(b"input")
        else:
            next(client.image.save("sample"))


@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
def test_invalid_timeout_is_not_a_refusal(family: str) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    with observation(policy()), pytest.raises(ValueError):
        if family == "captured":
            run(client.docker_cmd, timeout=0)
        elif family == "stream":
            stream_stdout_and_stderr(client.docker_cmd, timeout=0)
        elif family == "archive_load":
            client.image.load(b"input", timeout=0)
        else:
            client.image.save("sample", timeout=0)
    assert records == []


@pytest.mark.parametrize(
    "family", ["captured", "stream", "archive_load", "archive_save"]
)
def test_environment_resolution_error_is_not_a_refusal(family: str) -> None:
    records: list[TelemetryRecord] = []
    client = DockerClient(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    target = (
        "pywhales.components.image.cli_wrapper.resolve_environment"
        if family.startswith("archive_")
        else "pywhales.utils.resolve_environment"
    )
    with (
        patch.object(client.image, "inspect", return_value=[]),
        patch(target, side_effect=ValueError("private-environment")),
        observation(policy()),
        pytest.raises(ObservationLimitError) as raised,
    ):
        if family == "captured":
            run(client.docker_cmd)
        elif family == "stream":
            stream_stdout_and_stderr(client.docker_cmd)
        elif family == "archive_load":
            client.image.load(b"input")
        else:
            next(client.image.save("sample"))
    assert raised.value.reason == "environment"
    assert records == []


def test_admitted_refusal_correlates_and_observer_failure_preserves_error() -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("private-class")
    owner = AdmissionOwner(
        EndpointIdentity("private-endpoint"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )

    def observer(record: TelemetryRecord) -> None:
        records.append(record)
        raise RuntimeError("private-callback")

    client = DockerClient(client_call=[sys.executable], telemetry_observer=observer)
    original = ObservationCancelled()
    with (
        patch("pywhales._bounded_capture.check_scoped_launch", side_effect=original),
        observation(policy()),
        pytest.raises(ObservationCancelled) as raised,
    ):
        owner.request(pressure).call(lambda: run(client.docker_cmd))

    assert raised.value is original
    decision, refusal = records
    assert isinstance(decision, AdmissionDecisionRecord)
    assert isinstance(refusal, RefusalRecord)
    assert refusal.admission_id == decision.admission_id
    assert "private" not in json.dumps([asdict(record) for record in records])
