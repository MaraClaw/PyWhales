import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import MappingProxyType

import pytest

from pywhales.client_config import ClientConfig
from pywhales.exceptions import (
    DockerTimeout,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
    StreamBufferCategory,
    StreamBufferLimitError,
)
from pywhales.telemetry import (
    AdmissionDecisionRecord,
    AdmissionId,
    BoundedExporter,
    CompletionRecord,
    LaunchRecord,
    RefusalRecord,
    StopReason,
    TelemetryRecord,
)
from pywhales.telemetry._lifecycle import ExecutionTelemetry
from pywhales.telemetry._records import STOP_REASONS
from pywhales.utils import run


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (DockerTimeout(["private"], 1), "command_timeout"),
        (ObservationTimeout(), "scope_timeout"),
        (ObservationCancelled(), "cancelled"),
        (
            StreamBufferLimitError("private", "output_record"),
            "buffer_output_record",
        ),
        (
            StreamBufferLimitError("private", "stderr_history"),
            "buffer_stderr_history",
        ),
        (BufferError("private"), None),
        (ValueError("private"), None),
        (GeneratorExit(), None),
    ],
)
def test_completion_projects_the_exception_instance(
    error: BaseException, reason: StopReason | None
) -> None:
    records: list[TelemetryRecord] = []
    telemetry = ExecutionTelemetry(records.append, "archive_save")
    with (
        pytest.raises(type(error)),
        telemetry,
    ):
        telemetry.start()
        raise error
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert completed.stop_reason == reason
    telemetry.finish("closed")
    assert len(records) == 2


@pytest.mark.parametrize(
    "reason",
    [
        reason
        for reason in STOP_REASONS
        if reason
        not in {
            "command_timeout",
            "scope_timeout",
            "cancelled",
            "buffer_output_record",
            "buffer_stderr_history",
            "other_limit",
        }
    ],
)
def test_limit_instance_keeps_only_recognized_reason(reason: StopReason) -> None:
    records: list[TelemetryRecord] = []
    with (
        pytest.raises(ObservationLimitError),
        ExecutionTelemetry(records.append, "captured") as telemetry,
    ):
        telemetry.start()
        raise ObservationLimitError(reason, 1, 2)
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stop_reason) == ("limit", reason)


@pytest.mark.parametrize("reason", ["buffer_output_record", "buffer_stderr_history"])
def test_observation_limit_rejects_buffer_reasons(reason: str) -> None:
    records: list[TelemetryRecord] = []
    with (
        pytest.raises(ObservationLimitError),
        ExecutionTelemetry(records.append, "stream") as telemetry,
    ):
        telemetry.start()
        raise ObservationLimitError(reason, 1, 2)
    completed = records[1]
    assert isinstance(completed, CompletionRecord)
    assert (completed.outcome, completed.stop_reason) == ("limit", "other_limit")


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        ("output_record", "buffer_output_record"),
        ("stderr_history", "buffer_stderr_history"),
    ],
)
def test_buffer_limit_counts_use_their_own_keys(
    category: StreamBufferCategory, reason: StopReason
) -> None:
    exporter = BoundedExporter(
        lambda record: None, capacity=4, drop_policy="drop_newest"
    )
    telemetry = ExecutionTelemetry(exporter, "stream")
    with (
        pytest.raises(StreamBufferLimitError),
        telemetry,
    ):
        telemetry.start()
        raise StreamBufferLimitError("private", category)
    counts = exporter.stop_reason_counts
    assert counts[reason] == 1 and sum(counts.values()) == 1


@pytest.mark.parametrize("preflight", [False, True])
def test_unknown_limit_reason_never_enters_payload(preflight: bool) -> None:
    records: list[TelemetryRecord] = []
    telemetry = ExecutionTelemetry(records.append, "captured")
    with (
        pytest.raises(ObservationLimitError),
        telemetry.preflight() if preflight else telemetry,
    ):
        if not preflight:
            telemetry.start()
        raise ObservationLimitError("private-unknown-reason", 1, 2)
    record = records[-1]
    assert isinstance(record, CompletionRecord | RefusalRecord)
    assert record.stop_reason == "other_limit"
    assert "private" not in repr(asdict(record))


def test_record_positional_measurements_and_absent_reason_stay_compatible() -> None:
    completed = CompletionRecord("captured", "success", 2.5, 3, 4, attempt_id="id")
    refused = RefusalRecord("stream", "limit", attempt_id="refused")
    assert (completed.stdout_bytes, completed.stderr_bytes) == (3, 4)
    assert completed.stop_reason is refused.stop_reason is None


@pytest.mark.parametrize("flush", [False, True])
def test_counts_include_repeats_drops_and_shutdown_without_recount(flush: bool) -> None:
    seen: list[Mapping[StopReason, int]] = []

    def sink(record: TelemetryRecord) -> None:
        seen.append(exporter.stop_reason_counts)
        raise RuntimeError("sink failed")

    exporter = BoundedExporter(sink, capacity=1, drop_policy="drop_newest")
    old = exporter.stop_reason_counts
    record = RefusalRecord("captured", "limit", attempt_id="id", stop_reason="argv")
    exporter(record)
    exporter(record)
    assert exporter.shutdown(flush=flush) == 0
    exporter(record)
    assert exporter.shutdown() == exporter.flush() == 0
    assert exporter.stop_reason_counts["argv"] == 3
    assert old == dict.fromkeys(STOP_REASONS, 0)
    assert isinstance(old, MappingProxyType)
    assert exporter.dropped == (2 if flush else 3)
    assert exporter.export_errors == int(flush)
    assert [snapshot["argv"] for snapshot in seen] == ([2] if flush else [])


def test_concurrent_offers_have_exact_counts_for_every_reason() -> None:
    exporter = BoundedExporter(
        lambda record: None, capacity=1, drop_policy="drop_newest"
    )

    def offer(reason: StopReason) -> None:
        for _ in range(100):
            exporter(
                CompletionRecord(
                    "stream", "limit", 0, attempt_id="id", stop_reason=reason
                )
            )
            exporter(
                RefusalRecord("captured", "limit", attempt_id="id", stop_reason=reason)
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(offer, STOP_REASONS))
    exporter(LaunchRecord("captured", attempt_id="id"))
    exporter(AdmissionDecisionRecord(AdmissionId("admission"), "owner_demand_full", 0))
    exporter(CompletionRecord("captured", "success", 0, attempt_id="id"))
    assert exporter.stop_reason_counts == dict.fromkeys(STOP_REASONS, 200)
    assert exporter.dropped == len(STOP_REASONS) * 200 + 2
    exporter.shutdown(flush=False)


def test_real_child_timeout_is_counted_when_completion_overflows() -> None:
    exporter = BoundedExporter(
        lambda record: None, capacity=1, drop_policy="drop_newest"
    )
    config = ClientConfig(client_call=[sys.executable], telemetry_observer=exporter)
    with pytest.raises(DockerTimeout) as failure:
        run(
            [*config.docker_cmd, "-c", "import threading; threading.Event().wait()"],
            timeout=0.1,
        )
    assert failure.value.return_code is not None
    assert (exporter.queued, exporter.dropped) == (1, 1)
    assert exporter.stop_reason_counts["command_timeout"] == 1
    assert sum(exporter.stop_reason_counts.values()) == 1
    exporter.shutdown(flush=False)
