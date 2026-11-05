import json
import subprocess
import sys
import threading
import traceback
from collections.abc import Mapping, Sequence
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pywhales import DockerClient
from pywhales._observation import observation
from pywhales._process_stream import ProcessStream
from pywhales.client_config import ClientConfig, ParsingError, bulk_reload
from pywhales.components.context.cli_wrapper import Context
from pywhales.convergence import Failure, ServiceTarget, capture_service, wait
from pywhales.exceptions import DockerException, ObservationLimitError
from pywhales.inventory import InspectionPolicy, InventoryRequest, capture_inventory
from pywhales.telemetry import CompletionRecord, TelemetryRecord
from pywhales.utils import run, stream_stdout_and_stderr

BASE = InspectionPolicy(max_references=1, max_argv_bytes=65536, max_response_bytes=1024)


def test_nested_siblings_charge_parent_and_allow_exact_limit() -> None:
    parent = replace(BASE, max_total_response_bytes=6)
    with observation(parent):
        with observation(BASE):
            assert run([sys.executable, "-c", "print('abc', end='')"]) == "abc"
        with observation(BASE):
            assert run([sys.executable, "-c", "print('def', end='')"]) == "def"
        with pytest.raises(ObservationLimitError), observation(BASE):
            run([sys.executable, "-c", "print('x', end='')"])


def test_policy_reuse_has_fresh_root_budget() -> None:
    policy = replace(BASE, max_total_response_bytes=1)
    for _ in range(2):
        with observation(policy):
            assert run([sys.executable, "-c", "print('x', end='')"]) == "x"


@pytest.mark.parametrize(
    "field",
    ["max_total_references", "max_total_response_bytes", "max_total_stream_bytes"],
)
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1"])
def test_optional_limits_reject_nonpositive_or_noninteger_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        replace(BASE, **{field: value})


def test_reference_preflight_counts_duplicates_before_normalization_and_lookup() -> (
    None
):
    config = ClientConfig()
    request = InventoryRequest(
        kind="context", references=("secret", "secret"), client_config=config
    )
    with (
        observation(replace(BASE, max_total_references=1)),
        patch("pywhales._inventory_staging._normalize") as normalize,
        patch("shutil.which") as lookup,
        pytest.raises(ObservationLimitError) as failure,
    ):
        capture_inventory([request], policy=BASE)
    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "total_references",
        1,
        2,
    )
    assert "secret" not in "".join(traceback.format_exception(failure.value))
    normalize.assert_not_called()
    lookup.assert_not_called()


def test_reference_limit_is_per_inventory_and_duplicates_are_detached() -> None:
    config = ClientConfig(
        _client_call_with_path=[sys.executable, "-c", 'print(\'[ {"Name":"x"} ]\')']
    )
    request = InventoryRequest(
        kind="context", references=("x", "x"), client_config=config
    )
    policy = replace(BASE, max_total_references=2)
    with observation(policy):
        first = capture_inventory([request], policy=BASE)
        second = capture_inventory([request], policy=BASE)
    assert [item.id for item in first.records] == ["x", "x"]
    assert first.records[0].payload is not first.records[1].payload
    assert len(second.records) == 2


def test_failed_command_still_consumes_parent_capacity() -> None:
    with observation(replace(BASE, max_total_response_bytes=4)):
        with pytest.raises(DockerException), observation(BASE):
            run(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(2,b'abc'); raise SystemExit(1)",
                ]
            )
        with pytest.raises(ObservationLimitError), observation(BASE):
            run([sys.executable, "-c", "print('de',end='')"])


def test_tighter_child_overflow_still_charges_parent_and_later_sibling() -> None:
    with observation(replace(BASE, max_total_response_bytes=6)):
        with (
            observation(replace(BASE, max_total_response_bytes=2)),
            pytest.raises(ObservationLimitError),
        ):
            run([sys.executable, "-c", "print('abcd',end='')"])
        with observation(BASE):
            assert run([sys.executable, "-c", "print('abc',end='')"]) == "abc"
        with pytest.raises(ObservationLimitError), observation(BASE):
            run([sys.executable, "-c", "print('x',end='')"])


def test_per_command_overflow_bytes_remain_charged_to_scope() -> None:
    policy = replace(BASE, max_response_bytes=2, max_total_response_bytes=4)
    with observation(policy):
        with pytest.raises(ObservationLimitError) as failure:
            run([sys.executable, "-c", "print('abc',end='')"])
        assert failure.value.reason == "response"
        with pytest.raises(ObservationLimitError):
            run([sys.executable, "-c", "print('de',end='')"])


def test_exact_total_counts_stdout_and_stderr_raw_bytes() -> None:
    with observation(replace(BASE, max_total_response_bytes=4)):
        result = run(
            [sys.executable, "-c", "import os; os.write(1,b'ab'); os.write(2,b'cd')"],
            return_stderr=True,
        )
    assert result == ("ab", "cd")


def test_parse_failure_still_consumes_parent_capacity() -> None:
    config = ClientConfig(
        _client_call_with_path=[sys.executable, "-c", "print('bad',end='')"]
    )
    request = InventoryRequest(kind="context", references=("x",), client_config=config)
    with observation(replace(BASE, max_total_response_bytes=5)):
        with pytest.raises(ParsingError):
            capture_inventory([request], policy=BASE)
        with pytest.raises(ObservationLimitError):
            run([sys.executable, "-c", "print('abc',end='')"])


def test_later_bulk_chunk_overflow_preserves_cache_and_deduplicates() -> None:
    code = 'import json,sys; print(json.dumps([{"Name": value} for value in sys.argv[3:]]))'
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    first = Context(config, "a", is_immutable_id=True)
    second = Context(config, "b", is_immutable_id=True)
    before = [
        (item._inspect_result, item._last_refreshed_time) for item in (first, second)
    ]
    response_size = len(json.dumps([{"Name": "a"}]).encode()) + 1
    with pytest.raises(ObservationLimitError):
        bulk_reload(
            [first, first, second],
            policy=replace(
                BASE, max_total_references=2, max_total_response_bytes=response_size
            ),
        )
    assert [
        (item._inspect_result, item._last_refreshed_time) for item in (first, second)
    ] == before


def test_aggregate_overflow_reaps_child_and_reports_actual_bytes() -> None:
    records: list[TelemetryRecord] = []
    children: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def launch(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        children.append(process)
        return process

    config = ClientConfig(
        client_call=[sys.executable], telemetry_observer=records.append
    )
    with observation(replace(BASE, max_total_response_bytes=4)):
        run([*config.docker_cmd, "-c", "print('ab',end='')"])
        with (
            patch("pywhales._bounded_capture.subprocess.Popen", side_effect=launch),
            pytest.raises(ObservationLimitError) as failure,
        ):
            run(
                [
                    *config.docker_cmd,
                    "-c",
                    "import os,threading; os.write(2,b'X'*8); threading.Event().wait()",
                ]
            )
    assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
        "total_response",
        4,
        5,
    )
    assert "XXXXXXXX" not in "".join(traceback.format_exception(failure.value))
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout is not None and children[0].stdout.closed
    assert children[0].stderr is not None and children[0].stderr.closed
    assert isinstance(records[-1], CompletionRecord)
    assert (records[-1].stdout_bytes, records[-1].stderr_bytes) == (0, 3)


def test_wait_cycles_share_budget(tmp_path: Path) -> None:
    service = {
        "ID": "s",
        "Version": {"Index": 1},
        "Spec": {
            "Mode": {"Replicated": {"Replicas": 1}},
            "TaskTemplate": {"ContainerSpec": {"Image": "image"}},
        },
    }
    payload = json.dumps([service]) + "\n"
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import pathlib,sys\n"
        "root=pathlib.Path(__file__).parent\n"
        "with (root/'calls').open('a') as stream: stream.write(' '.join(sys.argv[1:])+'\\n')\n"
        f"if sys.argv[1:3] == ['service','inspect']: sys.stdout.write({payload!r})\n"
    )
    client = DockerClient(client_call=[sys.executable, str(script)])
    target = capture_service(client, "s", policy=BASE, timeout=3)
    assert isinstance(target, ServiceTarget)
    (tmp_path / "calls").unlink()
    result = wait(
        target,
        policy=replace(BASE, max_total_response_bytes=2 * len(payload.encode())),
        timeout=3,
        poll_interval=0.01,
    )
    assert isinstance(result, Failure) and result.reason == "observation"
    assert isinstance(result.cause, ObservationLimitError)
    assert result.cause.reason == "total_response"
    assert (tmp_path / "calls").read_text().splitlines().count("service inspect s") == 3


def budget(limit: int | None) -> InspectionPolicy:
    return replace(BASE, max_total_stream_bytes=limit)


def stream_of(code: str, *, pipe_stdin: bool = False) -> ProcessStream:
    return stream_stdout_and_stderr([sys.executable, "-c", code], pipe_stdin=pipe_stdin)


def assert_reaped(stream: ProcessStream) -> None:
    """Check the local child was killed and reaped and every owned thread joined."""
    assert stream._released.wait(3)
    assert stream._process.returncode is not None
    assert not stream._watchdog.is_alive()
    assert all(not thread.is_alive() for thread in stream._threads)
    assert all(
        pipe is None or pipe.closed
        for pipe in (stream._process.stdout, stream._process.stderr)
    )


STALLED_FLOOD = "import os,threading; os.write(1,b'x'*100); threading.Event().wait()"


def test_stream_budget_allows_an_exact_total_across_stdout_and_stderr() -> None:
    with observation(budget(8)) as state:
        stream = stream_of("import os; os.write(1,b'abc\\n'); os.write(2,b'def\\n')")
        with stream:
            records = sorted(stream)

    assert records == [("stderr", b"def\n"), ("stdout", b"abc\n")]
    assert state.stream_accounts[0].consumed == 8


def test_stream_budget_rejects_the_first_byte_past_it_and_reaps_the_child() -> None:
    raised: list[threading.ExceptHookArgs] = []
    with (
        patch.object(threading, "excepthook", raised.append),
        observation(budget(10)),
    ):
        stream = stream_of(STALLED_FLOOD)
    try:
        with pytest.raises(ObservationLimitError) as failure:
            list(stream)
        assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
            "total_stream",
            10,
            11,
        )
        assert_reaped(stream)
        assert list(stream) == []
        stream.close()
    finally:
        stream.close()
    assert raised == []


def test_stream_budget_counts_stderr_alone() -> None:
    with observation(budget(5)):
        stream = stream_of(
            "import os,threading; os.write(2,b'errors'); threading.Event().wait()"
        )
    with stream, pytest.raises(ObservationLimitError):
        list(stream)
    assert_reaped(stream)


def test_paused_stream_overflow_reaps_the_child_without_a_consumer() -> None:
    with observation(budget(10)):
        stream = stream_of(STALLED_FLOOD)
    try:
        assert_reaped(stream)

        with pytest.raises(ObservationLimitError):
            next(stream)
        assert list(stream) == []
    finally:
        stream.close()


def test_close_after_stream_overflow_leaves_the_error_for_one_delivery() -> None:
    with observation(budget(10)):
        stream = stream_of(STALLED_FLOOD)
    assert stream._released.wait(3)

    stream.close()
    with pytest.raises(ObservationLimitError):
        next(stream)

    assert list(stream) == []
    assert_reaped(stream)


def test_context_exit_delivers_a_stream_overflow_once() -> None:
    with observation(budget(10)):
        stream = stream_of(STALLED_FLOOD)

    with pytest.raises(ObservationLimitError), stream:
        assert stream._released.wait(3)

    assert list(stream) == []
    assert_reaped(stream)


def test_budget_only_scope_outlives_its_block_without_a_timer() -> None:
    code = "import os; os.write(1,b'ab\\n'); input(); os.write(1,b'cdefgh\\n')"
    with observation(budget(4)) as state:
        stream = stream_of(code, pipe_stdin=True)
    try:
        assert next(stream) == ("stdout", b"ab\n")
        assert stream._observation is state
        assert stream._wake_in() is None

        assert stream.stdin is not None
        stream.stdin.write(b"\n")
        stream.stdin.flush()
        with pytest.raises(ObservationLimitError) as failure:
            list(stream)

        assert (failure.value.limit, failure.value.measured) == (4, 5)
        assert_reaped(stream)
    finally:
        stream.close()


def test_stream_without_a_total_keeps_no_scope_and_is_not_limited() -> None:
    with observation(budget(None)):
        stream = stream_of("import os; os.write(1,b'x'*200000)")
    with stream:
        assert sum(len(payload) for _, payload in stream) == 200000
    assert stream._observation is None


def test_nested_scope_cannot_widen_an_outer_stream_budget() -> None:
    with observation(budget(10)), observation(budget(100)) as inner:
        stream = stream_of(STALLED_FLOOD)
        with stream, pytest.raises(ObservationLimitError) as failure:
            list(stream)

    assert (failure.value.limit, failure.value.measured) == (10, 11)
    assert [account.consumed for account in inner.stream_accounts] == [11, 11]


def test_tighter_inner_overflow_still_charges_the_outer_budget() -> None:
    with observation(budget(100)), observation(budget(5)) as inner:
        stream = stream_of(STALLED_FLOOD)
        with stream, pytest.raises(ObservationLimitError) as failure:
            list(stream)

    assert (failure.value.limit, failure.value.measured) == (5, 6)
    assert [account.consumed for account in inner.stream_accounts] == [6, 6]


def test_sibling_streams_share_the_enclosing_account() -> None:
    code = "import os; os.write(1,b'abcde\\n')"
    with observation(budget(10)):
        with observation(BASE), stream_of(code) as first:
            assert list(first) == [("stdout", b"abcde\n")]
        with (
            observation(BASE),
            stream_of(code) as second,
            pytest.raises(ObservationLimitError) as failure,
        ):
            list(second)

    assert (failure.value.limit, failure.value.measured) == (10, 11)


def test_concurrent_streams_charge_one_shared_account() -> None:
    workers, size = 4, 4
    results: list[int | ObservationLimitError] = []

    def follow() -> None:
        stream = stream_of("import os; os.write(1,b'abc\\n')")
        try:
            with stream:
                results.append(sum(len(payload) for _, payload in stream))
        except ObservationLimitError as error:
            results.append(error)

    with observation(budget(workers * size - 1)) as state:
        threads = [
            threading.Thread(target=copy_context().run, args=(follow,))
            for _ in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

    errors = [item for item in results if isinstance(item, ObservationLimitError)]
    delivered = [item for item in results if isinstance(item, int)]
    assert len(errors) == 1
    assert sum(delivered) == (workers - 1) * size
    assert state.stream_accounts[0].consumed == workers * size


def test_captured_output_is_never_charged_to_the_stream_budget() -> None:
    with observation(replace(budget(1), max_response_bytes=64)) as state:
        assert run([sys.executable, "-c", "print('x'*20, end='')"]) == "x" * 20

    assert state.stream_accounts[0].consumed == 0


def test_stream_output_is_never_charged_to_the_response_budget() -> None:
    with observation(replace(BASE, max_total_response_bytes=1)) as state:
        stream = stream_of("print('x'*20)")
        with stream:
            assert list(stream) == [("stdout", b"x" * 20 + b"\n")]

    assert state.response_accounts[0].consumed == 0


def test_inventory_capture_is_never_charged_to_the_stream_budget() -> None:
    config = ClientConfig(
        _client_call_with_path=[sys.executable, "-c", 'print(\'[ {"Name":"x"} ]\')']
    )
    request = InventoryRequest(kind="context", references=("x",), client_config=config)
    with observation(budget(1)) as state:
        snapshot = capture_inventory([request], policy=BASE)

    assert [item.id for item in snapshot.records] == ["x"]
    assert state.stream_accounts[0].consumed == 0


def aggregate(limit: int | None, **bounds: int) -> InspectionPolicy:
    return replace(BASE, max_total_io_bytes=limit, **bounds)


def test_aggregate_total_allows_an_exact_total_across_captured_and_stream_output() -> (
    None
):
    with observation(aggregate(8)) as state:
        assert run([sys.executable, "-c", "import os; os.write(1,b'abcd')"]) == "abcd"
        with stream_of("import os; os.write(2,b'efg\\n')") as stream:
            assert list(stream) == [("stderr", b"efg\n")]

    assert state.io_accounts[0].consumed == 8


def test_aggregate_overflow_by_one_byte_stops_stream_capture_and_upload_alike() -> None:
    raised: list[threading.ExceptHookArgs] = []
    count = "import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"
    before = set(threading.enumerate())
    with (
        patch.object(threading, "excepthook", raised.append),
        observation(aggregate(10, max_total_stdin_bytes=100)) as state,
    ):
        flood = stream_of(STALLED_FLOOD)
        with flood, pytest.raises(ObservationLimitError) as first:
            list(flood)
        assert_reaped(flood)
        assert list(flood) == []

        # An overflowed account is never spent again, by any family.
        with stream_of("print('z')") as later, pytest.raises(ObservationLimitError):
            list(later)
        assert_reaped(later)
        with pytest.raises(ObservationLimitError) as captured:
            run([sys.executable, "-c", "import os; os.write(1,b'x')"])
        with pytest.raises(ObservationLimitError) as uploaded:
            run([sys.executable, "-c", count], input=b"x")

    for failure in (first, captured, uploaded):
        assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
            "total_io",
            10,
            11,
        )
    assert state.io_accounts[0].consumed == 11
    assert state.stdin_accounts[0].consumed == 0
    assert raised == []
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    ("bounds", "reason"),
    [
        ({"max_response_bytes": 5}, "response"),
        ({"max_total_response_bytes": 5}, "total_response"),
    ],
)
def test_captured_source_limits_win_a_same_read_tie_with_the_aggregate(
    bounds: dict[str, int], reason: str
) -> None:
    with (
        observation(aggregate(5, **bounds)) as state,
        pytest.raises(ObservationLimitError) as failure,
    ):
        run([sys.executable, "-c", "import os; os.write(1,b'x'*50)"])

    assert failure.value.reason == reason
    assert state.io_accounts[0].consumed == 6


def test_stream_budget_wins_a_same_read_tie_with_the_aggregate() -> None:
    with observation(aggregate(5, max_total_stream_bytes=5)) as state:
        stream = stream_of(STALLED_FLOOD)
    with stream, pytest.raises(ObservationLimitError) as failure:
        list(stream)

    assert (failure.value.reason, failure.value.limit) == ("total_stream", 5)
    assert state.stream_accounts[0].consumed == state.io_accounts[0].consumed == 6
    assert_reaped(stream)


@pytest.mark.parametrize(("outer", "inner", "effective"), [(100, 5, 5), (10, 100, 10)])
def test_nested_aggregate_scopes_charge_every_enclosing_account_and_never_widen(
    outer: int, inner: int, effective: int
) -> None:
    with observation(aggregate(outer)), observation(aggregate(inner)) as scope:
        stream = stream_of(STALLED_FLOOD)
        with stream, pytest.raises(ObservationLimitError) as failure:
            list(stream)

    assert (failure.value.limit, failure.value.measured) == (effective, effective + 1)
    assert [account.consumed for account in scope.io_accounts] == [effective + 1] * 2


def test_aggregate_only_scope_outlives_its_block_without_a_timer_or_direct_stdin() -> (
    None
):
    code = "import os; os.write(1,b'ab\\n'); input(); os.write(1,b'cdefgh\\n')"
    with observation(aggregate(4)) as state:
        stream = stream_of(code, pipe_stdin=True)
    try:
        assert next(stream) == ("stdout", b"ab\n")
        assert stream._observation is state
        assert stream._wake_in() is None

        # Writing the child's stdin directly is outside the managed contract.
        assert stream.stdin is not None
        stream.stdin.write(b"\n")
        stream.stdin.flush()
        with pytest.raises(ObservationLimitError) as failure:
            list(stream)

        assert (failure.value.reason, failure.value.limit, failure.value.measured) == (
            "total_io",
            4,
            5,
        )
        assert state.io_accounts[0].consumed == 5
        assert_reaped(stream)
    finally:
        stream.close()
