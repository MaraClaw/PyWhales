from collections.abc import Callable, Iterator
from typing import Literal, assert_never, assert_type

from pywhales import Container, DockerClient
from pywhales.admission import (
    AdmissionOwner,
    AdmissionRequest,
    AdmissionSnapshot,
    AdmittedStream,
    ClassLimit,
    DemandSnapshot,
    EndpointIdentity,
    PressureClass,
)
from pywhales.components.image.streams import MultiImageStream
from pywhales.telemetry import (
    AdmissionDecisionRecord,
    AdmissionId,
    AdmissionOutcome,
    CompletionRecord,
    LaunchRecord,
    RefusalOutcome,
    RefusalRecord,
    TelemetryObserver,
    TelemetryRecord,
)
from pywhales.utils import ProcessStream


def _check_admission_generic_inference(
    owner: AdmissionOwner,
    pressure: PressureClass,
    client: DockerClient,
    operations: Iterator[Callable[[], Container]],
    multi: MultiImageStream,
    process: ProcessStream,
) -> None:
    def integer() -> int:
        return 1

    assert_type(owner.request(pressure), AdmissionRequest)
    assert_type(owner.request(pressure).call(lambda: client.create("image")), Container)
    assert_type(owner.request(pressure).call(integer), int)
    assert_type(owner.iter_calls(pressure, operations), Iterator[Container])
    assert_type(
        owner.request(pressure).stream(lambda: process),
        AdmittedStream[tuple[str, bytes]],
    )
    assert_type(
        owner.request(pressure).stream(lambda: multi), AdmittedStream[tuple[str, bytes]]
    )
    assert_type(next(owner.request(pressure).stream(lambda: multi)), tuple[str, bytes])
    assert_type(owner.snapshot(), AdmissionSnapshot)
    assert_type(owner.snapshot().closed, bool)
    assert_type(owner.snapshot().classes[0].outcomes.rejected_closed, int)
    assert_type(owner.close(), None)
    assert_type(owner.wait_drained(timeout=1), bool)
    assert_type(owner.request(pressure, demand=2).try_resize(3), bool)
    assert_type(owner.request(pressure).stream(lambda: process).try_resize(2), bool)
    assert_type(owner.snapshot().classes[0].demand, DemandSnapshot)


def _check_decision_telemetry(
    pressure: PressureClass, observer: TelemetryObserver, record: TelemetryRecord
) -> None:
    assert_type(
        AdmissionOwner(
            EndpointIdentity("local"),
            {pressure: ClassLimit(1, 0)},
            telemetry_observer=observer,
        ),
        AdmissionOwner,
    )
    match record:
        case AdmissionDecisionRecord():
            assert_type(record.admission_id, AdmissionId)
            assert_type(record.outcome, AdmissionOutcome)
            assert_type(record.duration_seconds, float)
            assert_type(record.phase, Literal["admission"])
            assert_type(record.schema_version, Literal[6])
        case LaunchRecord():
            assert_type(record.admission_id, AdmissionId | None)
            assert_type(record.schema_version, Literal[6])
        case CompletionRecord():
            assert_type(record.schema_version, Literal[6])
        case RefusalRecord():
            assert_type(record.admission_id, AdmissionId | None)
            assert_type(record.outcome, RefusalOutcome)
            assert_type(record.schema_version, Literal[6])
        case unreachable:
            assert_never(unreachable)
