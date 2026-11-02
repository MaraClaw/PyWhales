import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from types import TracebackType
from typing import override

import pytest

from pywhales.admission import (
    AdmissionOwner,
    ClassLimit,
    EndpointIdentity,
    PressureClass,
)
from pywhales.client_config import ClientConfig
from pywhales.telemetry import AdmissionDecisionRecord, LaunchRecord, TelemetryRecord
from pywhales.utils import run


@pytest.mark.parametrize("inner_observed", [False, True])
def test_nested_calls_stamp_innermost_admission_and_restore_outer_scope(
    inner_observed: bool,
) -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("read")
    outer = AdmissionOwner(
        EndpointIdentity("outer"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )
    inner = AdmissionOwner(
        EndpointIdentity("inner"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append if inner_observed else None,
    )
    config = ClientConfig(
        client_call=[sys.executable, "-c", "print('result')"],
        telemetry_observer=records.append,
    )

    def work() -> tuple[str, str, str]:
        before = run(config.docker_cmd)
        nested = inner.request(pressure).call(lambda: run(config.docker_cmd))
        after = run(config.docker_cmd)
        return before, nested, after

    assert outer.request(pressure).call(work) == ("result", "result", "result")
    assert run(config.docker_cmd) == "result"

    decisions = [r for r in records if isinstance(r, AdmissionDecisionRecord)]
    launches = [r for r in records if isinstance(r, LaunchRecord)]
    assert len(decisions) == (2 if inner_observed else 1)
    outer_id = decisions[0].admission_id
    inner_id = decisions[1].admission_id if inner_observed else None
    assert outer_id != inner_id
    assert [r.admission_id for r in launches] == [outer_id, inner_id, outer_id, None]


@pytest.mark.parametrize("ending", ["exhaust", "close", "body", "failure"])
def test_stream_launches_correlate_factory_entry_reads_and_cross_thread_exit(
    ending: str,
) -> None:
    records: list[TelemetryRecord] = []
    pressure = PressureClass("stream")
    owner = AdmissionOwner(
        EndpointIdentity("local"),
        {pressure: ClassLimit(1, 0)},
        telemetry_observer=records.append,
    )
    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    phases: list[str] = []
    original = ValueError("private-stream-failure")

    def launch(phase: str) -> None:
        phases.append(run([*config.docker_cmd, "-c", f"print({phase!r})"]))

    class Source(AbstractContextManager[Iterator[int]], Iterator[int]):
        def __init__(self) -> None:
            self.values = iter([7, 8])

        @override
        def __enter__(self) -> Iterator[int]:
            launch("entry")
            return self

        @override
        def __iter__(self) -> Iterator[int]:
            return self

        @override
        def __next__(self) -> int:
            launch("next")
            if ending == "failure":
                raise original
            return next(self.values)

        @override
        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            launch("exit")

    def factory() -> Source:
        launch("factory")
        return Source()

    stream = owner.request(pressure).stream(factory)
    try:
        if ending == "failure":
            with pytest.raises(ValueError) as raised:
                next(stream)
            assert raised.value is original
        elif ending == "body":
            with pytest.raises(ValueError) as raised, stream:
                raise original
            assert raised.value is original
        elif ending == "exhaust":
            assert list(stream) == [7, 8]
        else:
            with ThreadPoolExecutor(1) as executor:
                assert executor.submit(next, stream).result(3) == 7
                executor.submit(stream.close).result(3)
    finally:
        stream.close()
    launch("outside")

    decisions = [r for r in records if isinstance(r, AdmissionDecisionRecord)]
    launches = [r for r in records if isinstance(r, LaunchRecord)]
    assert len(decisions) == 1
    expected_nexts = {"exhaust": 3, "close": 1, "body": 0, "failure": 1}[ending]
    assert phases == ["factory", "entry", *["next"] * expected_nexts, "exit", "outside"]
    assert [r.admission_id for r in launches] == [
        *[decisions[0].admission_id] * (3 + expected_nexts),
        None,
    ]
    assert owner.snapshot().classes[0].active == 0
