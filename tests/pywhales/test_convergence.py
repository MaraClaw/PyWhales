import json
import logging
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, asdict, dataclass, field, fields, replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from pywhales import DockerClient
from pywhales._observation import ObservationState
from pywhales.convergence import (
    Cancelled,
    ComposeService,
    ComposeTarget,
    Deadline,
    DeadlineDetail,
    Failure,
    ServiceTarget,
    StackTarget,
    Success,
    capture_compose,
    capture_service,
    wait,
)
from pywhales.convergence._service import ServiceObservation
from pywhales.inventory import InspectionPolicy


@pytest.fixture
def policy() -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=65536
    )


@pytest.fixture
def fake_cli(tmp_path: Path) -> DockerClient:
    script = tmp_path / "docker.py"
    script.write_text("""import json, pathlib, sys, threading
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / "calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
data = json.loads((root / "data.json").read_text())
calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
hang = data.get("hang_at")
if hang and args == hang["args"] and calls.count(args) == hang["occurrence"]:
    (root / "started").write_text(str(__import__("os").getpid()))
    threading.Event().wait()
tasks = data["tasks"]
for identity, sequence in data.get("task_sequences", {}).items():
    count = sum(call[:2] == ["service", "ps"] and call[-1] == identity for call in calls)
    tasks = [item for item in tasks if item["ServiceID"] != identity] + sequence[min(max(count - 1, 0), len(sequence) - 1)]
if args[:2] == ["service", "inspect"]:
    sequence = data.get("sequences", {}).get(args[2])
    calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
    count = sum(call[:2] == ["service", "inspect"] and call[2] == args[2] for call in calls)
    print(json.dumps([sequence[min(count - 1, len(sequence) - 1)] if sequence else data["services"][args[2]]]))
elif args[:2] == ["service", "ps"]:
    if (root / "hang").exists():
        (root / "started").touch()
        threading.Event().wait()
    print("\\n".join(task["ID"] for task in tasks if task["ServiceID"] == args[-1]))
elif args[:3] == ["inspect", "--type", "task"]:
    override = data.get("task_inspect_overrides", {}).get(args[3])
    print(json.dumps([override] if override is not None else [task for task in tasks if task["ID"] == args[3]]))
elif args[0] == "compose" or args[:3] == ["inspect", "--type", "container"]:
    snapshots = data["compose"]
    cycle = sum(call[0] == "compose" for call in calls)
    snapshot = snapshots[min(cycle, len(snapshots)) - 1]
    members = snapshot["containers"]
    if args[0] == "compose":
        selected = args[args.index("--quiet") + 1:]
        label = "com.docker.compose.service"
        print(snapshot["raw_listing"] if "raw_listing" in snapshot else "\\n".join(
            identity for identity in snapshot["listing"]
            if identity not in members or not selected or members[identity]["Config"]["Labels"][label] in selected
        ))
    elif args[3] in snapshot.get("raw", {}):
        print(snapshot["raw"][args[3]])
    elif args[3] in members:
        print(json.dumps([members[args[3]]]))
    else:
        sys.stderr.write("Error: No such container: " + args[3] + "\\n")
        sys.exit(1)
elif args[:2] in (["stack", "deploy"], ["service", "update"]):
    pass
else:
    sys.exit(2)
""")
    return DockerClient(client_call=[sys.executable, str(script)])


def service(identity: str = "service-a", replicas: int = 1) -> dict[str, Any]:
    return {
        "ID": identity,
        "Version": {"Index": 5},
        "Spec": {
            "Mode": {"Replicated": {"Replicas": replicas}},
            "TaskTemplate": {
                "ContainerSpec": {
                    "Image": "example:1",
                    "Mounts": [{"Target": "/data", "Future": {"A": 2}}],
                },
                "ForceUpdate": 3,
            },
        },
    }


def task(
    identity: str = "task-a", service_id: str = "service-a", slot: int = 1
) -> dict[str, Any]:
    return {
        "ID": identity,
        "ServiceID": service_id,
        "Slot": slot,
        "DesiredState": "running",
        "Status": {"State": "running"},
        "Spec": {**service()["Spec"]["TaskTemplate"], "Runtime": "container"},
    }


def store(
    path: Path, services: list[dict[str, Any]], tasks: list[dict[str, Any]]
) -> None:
    (path / "data.json").write_text(
        json.dumps(
            {"services": {item["ID"]: item for item in services}, "tasks": tasks}
        )
    )


def capture(
    client: DockerClient, policy: InspectionPolicy, identity: str = "service-a"
) -> ServiceTarget:
    result = capture_service(client, identity, policy=policy, timeout=2)
    assert isinstance(result, ServiceTarget)
    return result


def test_matching_template_converges_via_fake_cli(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(tmp_path, [service()], [task()])
    target = capture(fake_cli, policy)
    assert isinstance(
        wait(target, policy=policy, timeout=2, poll_interval=0.01), Success
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls == [
        ["service", "inspect", "service-a"],
        ["service", "inspect", "service-a"],
        ["service", "ps", "--quiet", "--no-trunc", "service-a"],
        ["inspect", "--type", "task", "task-a"],
        ["service", "inspect", "service-a"],
    ]


@pytest.mark.parametrize("mutation", ["stale", "missing", "duplicate", "history"])
def test_insufficient_current_task_evidence_never_succeeds(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, mutation: str
) -> None:
    current = task()
    tasks = [current]
    if mutation == "stale":
        current["Spec"]["ContainerSpec"]["Mounts"][0]["Future"]["A"] = 9
    elif mutation == "missing":
        del current["Spec"]
    elif mutation == "duplicate":
        tasks.append(task("task-b"))
    else:
        current["DesiredState"] = "shutdown"
    store(tmp_path, [service()], tasks)
    result = wait(
        capture(fake_cli, policy), policy=policy, timeout=0.2, poll_interval=1
    )
    assert isinstance(result, Deadline)


@pytest.mark.parametrize(
    "state,expected",
    [
        ("paused", "update_failed"),
        ("rollback_started", "update_failed"),
        ("rollback_paused", "update_failed"),
        ("rollback_completed", "update_failed"),
        ("failed", "update_failed"),
        ("mystery", "malformed"),
    ],
)
def test_failed_update_states_are_terminal(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    state: str,
    expected: str,
) -> None:
    snapshot = service()
    snapshot["UpdateStatus"] = {"State": state}
    store(tmp_path, [snapshot], [task()])
    result = wait(
        capture(fake_cli, policy), policy=policy, timeout=2, poll_interval=0.01
    )
    assert (
        isinstance(result, Failure)
        and result.reason == expected
        and result.state == (None if state == "mystery" else state)
    )


@pytest.mark.parametrize("state", [None, "completed", "updating"])
def test_update_progress_state(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, state: str | None
) -> None:
    snapshot = service()
    if state is not None:
        snapshot["UpdateStatus"] = {"State": state}
    store(tmp_path, [snapshot], [task()])
    result = wait(
        capture(fake_cli, policy),
        policy=policy,
        timeout=0.4 if state == "updating" else 3,
        poll_interval=1,
    )
    assert isinstance(result, Deadline if state == "updating" else Success)


@pytest.mark.parametrize(
    "mode",
    [
        {"Global": {}},
        {"ReplicatedJob": {"MaxConcurrent": 0}},
        {"GlobalJob": {}},
        {},
        {"Replicated": {}},
        {"Replicated": {"Replicas": -1}},
    ],
)
def test_unsupported_modes_fail_closed(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    mode: dict[str, Any],
) -> None:
    snapshot = service()
    snapshot["Spec"]["Mode"] = mode
    store(tmp_path, [snapshot], [])
    result = capture_service(fake_cli, "service-a", policy=policy, timeout=2)
    assert isinstance(result, Failure) and result.reason == "unsupported"


def test_zero_and_fixed_stack_membership(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(
        tmp_path,
        [service(replicas=0), service("service-b")],
        [task(service_id="service-b")],
    )
    target = StackTarget(
        services=(capture(fake_cli, policy), capture(fake_cli, policy, "service-b"))
    )
    assert isinstance(
        wait(target, policy=policy, timeout=2, poll_interval=0.01), Success
    )


def test_stack_rejects_mutable_membership(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(tmp_path, [global_service()], [])
    members: Any = [capture_global(fake_cli, policy)]
    with pytest.raises(ValueError):
        StackTarget(services=members)


def test_version_change_is_superseded(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = service()
    store(tmp_path, [snapshot], [task()])
    target = capture(fake_cli, policy)
    snapshot["Version"]["Index"] = 6
    store(tmp_path, [snapshot], [task()])
    result = wait(target, policy=policy, timeout=2, poll_interval=0.01)
    assert isinstance(result, Failure) and result.reason == "superseded"


@pytest.mark.parametrize("global_mode", [False, True])
def test_cancellation_reaps_hung_fake_cli(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_mode: bool,
) -> None:
    store(tmp_path, [global_service() if global_mode else service()], [task()])
    target = (
        capture_global(fake_cli, policy) if global_mode else capture(fake_cli, policy)
    )
    (tmp_path / "hang").touch()
    cancel = Event()
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen
    launched = Event()

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        if "ps" in args:
            launched.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", track)
    results = []
    worker = Thread(
        target=lambda: results.append(
            wait(target, policy=policy, timeout=5, poll_interval=1, cancel=cancel)
        )
    )
    worker.start()
    assert launched.wait(2)
    end = time.monotonic() + 2
    while not (tmp_path / "started").exists() and time.monotonic() < end:
        Event().wait(0.01)
    assert (tmp_path / "started").exists()
    cancel.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(results) == 1 and isinstance(results[0], Cancelled)
    assert processes[-1].returncode is not None
    assert processes[-1].stdout is not None and processes[-1].stdout.closed
    assert processes[-1].stderr is not None and processes[-1].stderr.closed
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls[-1][:2] == ["service", "ps"]


def test_deadline_reaps_hung_child(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store(tmp_path, [service()], [task()])
    target = capture(fake_cli, policy)
    (tmp_path / "hang").touch()
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track)
    result = wait(target, policy=policy, timeout=2, poll_interval=1)
    assert isinstance(result, Deadline)
    assert result.detail is None
    assert (tmp_path / "started").exists()
    assert processes[-1].returncode is not None
    assert processes[-1].stdout is not None and processes[-1].stdout.closed
    assert processes[-1].stderr is not None and processes[-1].stderr.closed
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls[-1][:2] == ["service", "ps"]


def test_stack_waits_for_every_fixed_service(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(tmp_path, [service(), service("service-b")], [task()])
    target = StackTarget(
        services=(capture(fake_cli, policy), capture(fake_cli, policy, "service-b"))
    )
    assert isinstance(
        wait(target, policy=policy, timeout=0.3, poll_interval=1), Deadline
    )


def test_template_change_without_version_change_is_superseded(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = service()
    store(tmp_path, [snapshot], [task()])
    target = capture(fake_cli, policy)
    snapshot["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"][0]["Future"]["A"] = 8
    store(tmp_path, [snapshot], [task()])
    result = wait(target, policy=policy, timeout=2, poll_interval=0.01)
    assert isinstance(result, Failure) and result.reason == "superseded"


def test_rollout_sequence_reaches_completed(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    updating = service()
    updating["UpdateStatus"] = {"State": "updating"}
    completed = service()
    completed["UpdateStatus"] = {"State": "completed"}
    store(tmp_path, [completed], [task()])
    data = json.loads((tmp_path / "data.json").read_text())
    data["sequences"] = {"service-a": [completed, updating, completed]}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(
        capture(fake_cli, policy), policy=policy, timeout=3, poll_interval=0.01
    )
    assert isinstance(result, Success)
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert sum(call[:2] == ["service", "ps"] for call in calls) == 2


def test_changed_mode_is_superseded(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = service()
    store(tmp_path, [snapshot], [])
    target = capture(fake_cli, policy)
    snapshot["Spec"]["Mode"] = {"Global": {}}
    store(tmp_path, [snapshot], [])
    result = wait(target, policy=policy, timeout=2, poll_interval=0.01)
    assert isinstance(result, Failure) and result.reason == "superseded"


def test_mutations_do_not_invoke_observation_or_rollback(
    fake_cli: DockerClient, tmp_path: Path
) -> None:
    store(tmp_path, [], [])
    fake_cli.stack.deploy("sample", [])
    fake_cli.service.update("service-a", detach=True)
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls == [
        ["stack", "deploy", "--resolve-image", "always", "sample"],
        ["service", "update", "--detach", "service-a"],
    ]


def test_fake_clock_deadline_stops_polling(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywhales._observation as observation_module
    import pywhales.convergence._wait as wait_module

    store(tmp_path, [service()], [])
    target = capture(fake_cli, policy)
    current = [100.0]
    polls = []

    def sleep(seconds: float) -> None:
        current[0] += seconds

    def observe(_: ServiceTarget) -> ServiceObservation:
        polls.append(current[0])
        return ServiceObservation(True, False, False, False, False, False)

    clock = SimpleNamespace(monotonic=lambda: current[0], sleep=sleep)
    monkeypatch.setattr(wait_module, "time", clock)
    monkeypatch.setattr(observation_module, "time", clock)
    monkeypatch.setattr(wait_module, "check_service", observe)
    result = wait(target, policy=policy, timeout=2, poll_interval=1)
    assert isinstance(result, Deadline)
    assert polls == [100.0, 101.0]


@pytest.mark.parametrize("counter", [item.name for item in fields(DeadlineDetail)])
@pytest.mark.parametrize("value", [-1, 2, True, 0.5])
def test_deadline_counts_reject_invalid_values(
    counter: str, value: int | float
) -> None:
    detail = DeadlineDetail(1)
    if counter == "captured_services" and type(value) is int and value == 2:
        assert replace(detail, captured_services=2).captured_services == 2
    else:
        with pytest.raises(ValueError):
            replace(detail, **{counter: value})


def test_deadline_detail_is_immutable_and_optional() -> None:
    detail = DeadlineDetail(2, missing_ready_slots=1, stale_templates=1)
    outcome = Deadline(detail)
    for attribute in fields(detail):
        with pytest.raises(FrozenInstanceError):
            setattr(detail, attribute.name, 2)
    for attribute in fields(outcome):
        with pytest.raises(FrozenInstanceError):
            setattr(outcome, attribute.name, None)
    assert Deadline().detail is None
    assert all(type(value) is int for value in asdict(detail).values())


@pytest.mark.parametrize("subclass", [False, True])
def test_deadline_rejects_payload_bearing_detail(subclass: bool) -> None:
    @dataclass(frozen=True, slots=True)
    class PayloadDetail(DeadlineDetail):
        wire: dict[str, str] = field(default_factory=dict)

    payload = {"secret": "credential-payload"}
    detail = PayloadDetail(1, wire=payload) if subclass else payload
    with pytest.raises(ValueError):
        replace(Deadline(), detail=detail)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("empty", DeadlineDetail(1, missing_ready_slots=1)),
        ("duplicate", DeadlineDetail(1, duplicate_slots=1)),
        ("stale", DeadlineDetail(1, missing_ready_slots=1, stale_templates=1)),
        ("nonrunning", DeadlineDetail(1, missing_ready_slots=1, non_running_tasks=1)),
        ("missing", DeadlineDetail(1, missing_ready_slots=1, invalid_task_shape=1)),
        ("slot", DeadlineDetail(1, missing_ready_slots=1, invalid_task_shape=1)),
        ("updating", DeadlineDetail(1, ongoing_updates=1)),
        ("history", DeadlineDetail(1, missing_ready_slots=1)),
        (
            "overlap",
            DeadlineDetail(
                1,
                missing_ready_slots=1,
                duplicate_slots=1,
                stale_templates=1,
                non_running_tasks=1,
                invalid_task_shape=1,
                ongoing_updates=1,
            ),
        ),
    ],
)
def test_each_deadline_reason_counts_services_not_tasks(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    mutation: str,
    expected: DeadlineDetail,
) -> None:
    snapshot, current = service(), task()
    tasks = [current]
    if mutation == "empty":
        tasks = []
    elif mutation == "duplicate":
        tasks.extend([task("task-b"), task("task-c")])
    elif mutation == "stale":
        current["Spec"]["ContainerSpec"]["Image"] = "credential-payload"
    elif mutation == "nonrunning":
        current["Status"]["State"] = "starting"
    elif mutation == "missing":
        del current["Spec"]
    elif mutation == "slot":
        current["Slot"] = 2
    elif mutation == "updating":
        snapshot["UpdateStatus"] = {"State": "updating"}
    elif mutation == "history":
        current["DesiredState"] = "shutdown"
        current["Slot"] = -1
        current["Status"]["State"] = "failed"
        current["Spec"]["ContainerSpec"]["Image"] = "credential-payload"
    elif mutation == "overlap":
        snapshot["UpdateStatus"] = {"State": "updating"}
        current["Slot"] = 2
        current["Status"]["State"] = "starting"
        current["Spec"]["ContainerSpec"]["Image"] = "credential-payload"
        tasks.append({**current, "ID": "task-b", "Spec": None})
    store(tmp_path, [snapshot], tasks)
    result = wait(capture(fake_cli, policy), policy=policy, timeout=2, poll_interval=10)
    assert isinstance(result, Deadline) and result.detail == expected
    assert "credential-payload" not in repr(result)
    assert "service-a" not in repr(result) and "task-a" not in repr(result)


def test_mixed_stack_summary_counts_only_affected_captured_services(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    updating = service("service-b")
    updating["UpdateStatus"] = {"State": "updating"}
    stale = task("task-b", "service-b")
    stale["Spec"]["ForceUpdate"] = 99
    history = task("history", "service-c", slot=-1)
    history["DesiredState"] = "shutdown"
    del history["Spec"]
    store(
        tmp_path,
        [service(), updating, service("service-c", replicas=0), service("service-d")],
        [task(), stale, history],
    )
    target = StackTarget(
        services=tuple(
            capture(fake_cli, policy, f"service-{suffix}") for suffix in "abcd"
        )
    )
    result = wait(target, policy=policy, timeout=3, poll_interval=10)
    assert isinstance(result, Deadline)
    assert result.detail == DeadlineDetail(
        4, missing_ready_slots=2, stale_templates=1, ongoing_updates=1
    )


@pytest.mark.parametrize(
    "stage",
    ["first", "later-member", "first-recheck", "later-recheck", "latest-member"],
)
def test_partial_cycles_never_replace_complete_detail_and_hung_children_are_reaped(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    store(tmp_path, [service(), service("service-b")], [task("task-b", "service-b")])
    target = StackTarget(
        services=(capture(fake_cli, policy), capture(fake_cli, policy, "service-b"))
    )
    data = json.loads((tmp_path / "data.json").read_text())
    later = stage.startswith(("later", "latest"))
    recheck = stage.endswith("recheck")
    data["task_sequences"] = {"service-a": [[], [task()]] if later else [[task()]]}
    command = (
        ["service", "inspect", "service-b"]
        if recheck
        else ["service", "ps", "--quiet", "--no-trunc", "service-b"]
    )
    occurrence = (4 if later else 3) if recheck else (2 if later else 1)
    expected = DeadlineDetail(2, missing_ready_slots=1) if later else None
    if stage == "latest-member":
        stale = task()
        stale["Spec"]["ForceUpdate"] = 99
        data["task_sequences"] = {"service-a": [[], [stale], []]}
        occurrence = 3
        expected = DeadlineDetail(2, missing_ready_slots=1, stale_templates=1)
    data["hang_at"] = {"args": command, "occurrence": occurrence}
    (tmp_path / "data.json").write_text(json.dumps(data))
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track)
    result = wait(target, policy=policy, timeout=3, poll_interval=0.01)
    assert isinstance(result, Deadline)
    assert result.detail == expected
    assert (tmp_path / "started").exists()
    assert all(process.returncode is not None for process in processes)
    assert all(
        process.stdout is not None and process.stdout.closed for process in processes
    )
    assert all(
        process.stderr is not None and process.stderr.closed for process in processes
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls[-1] == command and calls.count(command) == occurrence


@pytest.mark.parametrize("state", ["updating", "paused", "mystery", "superseded"])
def test_final_generation_recheck_precedes_publication_and_success(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    state: str,
) -> None:
    snapshot, changed = service(), service()
    if state == "superseded":
        changed["Version"]["Index"] += 1
    else:
        changed["UpdateStatus"] = {"State": state}
    store(tmp_path, [snapshot], [task()])
    target = capture(fake_cli, policy)
    data = json.loads((tmp_path / "data.json").read_text())
    data["sequences"] = {"service-a": [snapshot, snapshot, changed]}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(target, policy=policy, timeout=2, poll_interval=10)
    if state == "updating":
        assert isinstance(result, Deadline) and result.detail == DeadlineDetail(
            1, ongoing_updates=1
        )
    else:
        assert isinstance(result, Failure)
        assert (
            result.reason
            == {
                "paused": "update_failed",
                "mystery": "malformed",
                "superseded": "superseded",
            }[state]
        )


def test_expiry_at_final_checkpoint_discards_candidate(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywhales.convergence._wait as wait_module
    from pywhales.exceptions import ObservationTimeout

    store(tmp_path, [service()], [])
    target = capture(fake_cli, policy)
    check = wait_module.check_observation
    checkpoints = 0

    def expire(state: ObservationState | None = None) -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise ObservationTimeout()
        check(state)

    monkeypatch.setattr(wait_module, "check_observation", expire)
    result = wait(target, policy=policy, timeout=2, poll_interval=10)
    assert isinstance(result, Deadline) and result.detail is None
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls[-1][:2] == ["service", "ps"]


def test_final_stack_rechecks_continue_after_an_ongoing_update(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    first, second = global_service(), service("service-b")
    store(
        tmp_path,
        [first, second],
        [
            global_task("task-a", "node-a"),
            global_task("task-c", "node-b"),
            task("task-b", "service-b"),
        ],
    )
    target = StackTarget(
        services=(
            capture_global(fake_cli, policy),
            capture(fake_cli, policy, "service-b"),
        )
    )
    updating = {**first, "UpdateStatus": {"State": "updating"}}
    superseded = {**second, "Version": {"Index": 8}}
    data = json.loads((tmp_path / "data.json").read_text())
    data["sequences"] = {
        "service-a": [first, first, updating],
        "service-b": [second, second, superseded],
    }
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(target, policy=policy, timeout=3, poll_interval=10)
    assert isinstance(result, Failure) and result.reason == "superseded"
    assert result.service_id == "service-b"


@pytest.mark.parametrize("limit", ["max_argv_bytes", "max_response_bytes"])
def test_observation_limits_are_failures_not_deadline_detail(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, limit: str
) -> None:
    from pywhales.exceptions import ObservationLimitError

    store(tmp_path, [service()], [task()])
    target = capture(fake_cli, policy)
    bounded = replace(policy, **{limit: 1})
    result = wait(target, policy=bounded, timeout=2, poll_interval=10)
    assert isinstance(result, Failure) and result.reason == "observation"
    assert isinstance(result.cause, ObservationLimitError)


def test_capture_deadline_has_no_complete_cycle_detail(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(tmp_path, [service()], [])
    data = json.loads((tmp_path / "data.json").read_text())
    data["hang_at"] = {"args": ["service", "inspect", "service-a"], "occurrence": 1}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = capture_service(fake_cli, "service-a", policy=policy, timeout=2)
    assert isinstance(result, Deadline) and result.detail is None
    assert (tmp_path / "started").exists()


def global_service(identity: str = "service-a") -> dict[str, Any]:
    snapshot = service(identity)
    snapshot["Spec"]["Mode"] = {"Global": {}}
    return snapshot


def job_service(*, global_job: bool = False, iteration: int = 1) -> dict[str, Any]:
    snapshot = service()
    snapshot["Spec"]["Mode"] = (
        {"GlobalJob": {}} if global_job else {"ReplicatedJob": {"TotalCompletions": 2}}
    )
    snapshot["JobStatus"] = {"JobIteration": {"Index": iteration} if iteration else {}}
    return snapshot


def job_task(
    identity: str, slot: int, iteration: int, state: str = "complete"
) -> dict[str, Any]:
    current = task(identity, slot=slot)
    current["JobIteration"] = {"Index": iteration} if iteration else {}
    current["Status"]["State"] = state
    current["DesiredState"] = "shutdown"
    return current


@pytest.mark.parametrize("global_job", [False, True])
def test_job_current_iteration_completes_via_fake_cli(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, global_job: bool
) -> None:
    snapshot = job_service(global_job=global_job, iteration=0)
    first, second = job_task("a", 0, 0), job_task("b", 1, 0)
    first["NodeID"], second["NodeID"] = "node-a", "node-b"
    snapshot["UpdateStatus"] = {"State": "failed"}
    store(tmp_path, [snapshot], [first, second])
    target = (
        capture_global(fake_cli, policy) if global_job else capture(fake_cli, policy)
    )
    assert isinstance(wait(target, policy=policy, timeout=5, poll_interval=1), Success)


@pytest.mark.parametrize("stage", [1, 2])
def test_job_rerun_supersedes_at_cycle_or_final_recheck(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, stage: int
) -> None:
    snapshot = job_service()
    store(tmp_path, [snapshot], [job_task("a", 0, 1), job_task("b", 1, 1)])
    target = capture(fake_cli, policy)
    changed = job_service(iteration=2)
    data = json.loads((tmp_path / "data.json").read_text())
    data["sequences"] = {"service-a": [snapshot] * stage + [changed]}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(target, policy=policy, timeout=5, poll_interval=1)
    assert isinstance(result, Failure) and result.reason == "superseded"


def test_job_version_bump_is_not_a_new_generation(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = job_service()
    store(tmp_path, [snapshot], [job_task("a", 0, 1), job_task("b", 1, 1)])
    target = capture(fake_cli, policy)
    snapshot["Version"]["Index"] += 1
    store(tmp_path, [snapshot], [job_task("a", 0, 1), job_task("b", 1, 1)])
    assert isinstance(wait(target, policy=policy, timeout=5, poll_interval=1), Success)


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("none", "failure"),
        ("any", "deadline"),
        (None, "deadline"),
        ("future", "deadline"),
    ],
)
def test_job_failures_and_stale_tasks(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    condition: str | None,
    expected: str,
) -> None:
    snapshot = job_service()
    if condition is not None:
        snapshot["Spec"]["TaskTemplate"]["RestartPolicy"] = {"Condition": condition}
    store(tmp_path, [snapshot], [job_task("a", 0, 1, "failed"), job_task("old", 1, 0)])
    result = wait(capture(fake_cli, policy), policy=policy, timeout=2, poll_interval=5)
    if expected == "failure":
        assert isinstance(result, Failure) and result.reason == "completion_failed"
    else:
        assert isinstance(result, Deadline) and result.detail == DeadlineDetail(
            1, missing_ready_slots=1
        )


def test_job_retry_replacement_same_slot_is_not_duplicate(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(
        tmp_path,
        [job_service()],
        [job_task("a", 0, 1, "failed"), job_task("b", 0, 1), job_task("c", 1, 1)],
    )
    assert isinstance(
        wait(capture(fake_cli, policy), policy=policy, timeout=5, poll_interval=1),
        Success,
    )


def test_global_job_stale_completion_does_not_count(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    first, stale = job_task("a", 0, 1), job_task("b", 0, 0)
    first["NodeID"], stale["NodeID"] = "node-a", "node-b"
    store(tmp_path, [job_service(global_job=True)], [first, stale])
    result = wait(
        capture_global(fake_cli, policy), policy=policy, timeout=2, poll_interval=5
    )
    assert isinstance(result, Deadline)
    assert result.detail == DeadlineDetail(1, missing_ready_nodes=1)


def test_job_without_status_is_malformed(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    snapshot = job_service()
    del snapshot["JobStatus"]
    store(tmp_path, [snapshot], [])
    result = capture_service(fake_cli, "service-a", policy=policy, timeout=2)
    assert isinstance(result, Failure) and result.reason == "malformed"


def global_task(
    identity: str, node_id: str, service_id: str = "service-a"
) -> dict[str, Any]:
    current = task(identity, service_id)
    current.pop("Slot")
    current["NodeID"] = node_id
    return current


def capture_global(
    client: DockerClient, policy: InspectionPolicy, identity: str = "service-a"
) -> ServiceTarget:
    result = capture_service(
        client,
        identity,
        policy=policy,
        timeout=5,
        approved_node_ids=("node-a", "node-b"),
    )
    assert isinstance(result, ServiceTarget)
    return result


@pytest.mark.parametrize(
    "membership", [(), ("node-a", "node-a"), ("",), (1,), ["node-a"]]
)
def test_global_capture_rejects_invalid_membership_before_launch(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    membership: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        capture_service(
            fake_cli,
            "service-a",
            policy=policy,
            timeout=5,
            approved_node_ids=membership,
        )
    assert not (tmp_path / "calls").exists()


def test_capture_freezes_global_membership_and_rejects_replicated_override(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store(tmp_path, [global_service(), service("service-b")], [])
    target = capture_global(fake_cli, policy)
    assert target.mode == "global" and target.approved_node_ids == ("node-a", "node-b")
    result = capture_service(
        fake_cli, "service-b", policy=policy, timeout=5, approved_node_ids=("node-a",)
    )
    assert isinstance(result, Failure) and result.reason == "unsupported"


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("absent", DeadlineDetail(1, missing_ready_nodes=1)),
        (
            "missing",
            DeadlineDetail(1, missing_ready_nodes=1, missing_node_assignments=1),
        ),
        ("empty", DeadlineDetail(1, missing_ready_nodes=1, missing_node_assignments=1)),
        (
            "duplicate",
            DeadlineDetail(1, missing_ready_nodes=1, duplicate_node_assignments=1),
        ),
        ("extra-duplicate", DeadlineDetail(1, duplicate_node_assignments=1)),
        ("outside", DeadlineDetail(1, missing_ready_nodes=1, outside_approved_nodes=1)),
        ("extra-outside", DeadlineDetail(1, outside_approved_nodes=1)),
        ("stale", DeadlineDetail(1, missing_ready_nodes=1, stale_templates=1)),
        ("nonrunning", DeadlineDetail(1, missing_ready_nodes=1, non_running_tasks=1)),
        ("history", DeadlineDetail(1, missing_ready_nodes=1)),
    ],
)
def test_global_membership_blockers_are_safe_affected_service_counts(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    mutation: str,
    expected: DeadlineDetail,
) -> None:
    first, second = global_task("task-a", "node-a"), global_task("task-b", "node-b")
    tasks = [first, second]
    if mutation == "absent":
        tasks.pop()
    elif mutation == "missing":
        del second["NodeID"]
    elif mutation == "empty":
        second["NodeID"] = ""
    elif mutation == "duplicate":
        second["NodeID"] = "node-a"
    elif mutation == "extra-duplicate":
        tasks.append(global_task("task-c", "node-a"))
    elif mutation == "outside":
        second["NodeID"] = "private-outside-node"
    elif mutation == "extra-outside":
        tasks.append(global_task("task-c", "private-outside-node"))
    elif mutation == "stale":
        second["Spec"]["ContainerSpec"]["Image"] = "private-template"
    elif mutation == "nonrunning":
        second["Status"]["State"] = "starting"
    else:
        second["DesiredState"] = "shutdown"
    store(tmp_path, [global_service()], tasks)
    result = wait(
        capture_global(fake_cli, policy), policy=policy, timeout=4, poll_interval=10
    )
    assert isinstance(result, Deadline) and result.detail == expected
    assert "node-a" not in repr(result) and "private" not in repr(result)


@pytest.mark.parametrize("node_id", [None, 4, True, [], {}])
def test_global_malformed_node_wire_fails_safely(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, node_id: object
) -> None:
    current = global_task("task-a", "node-a")
    current["NodeID"] = node_id
    store(tmp_path, [global_service()], [current])
    result = wait(
        capture_global(fake_cli, policy), policy=policy, timeout=5, poll_interval=1
    )
    assert isinstance(result, Failure) and result.reason == "observation"
    assert "NodeID" not in repr(result)


@pytest.mark.parametrize("field", ["ID", "ServiceID"])
def test_global_mismatched_task_identity_fails_safely(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, field: str
) -> None:
    current = global_task("task-a", "node-a")
    store(tmp_path, [global_service()], [current])
    data = json.loads((tmp_path / "data.json").read_text())
    data["task_inspect_overrides"] = {"task-a": {**current, field: "wrong-id"}}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(
        capture_global(fake_cli, policy), policy=policy, timeout=5, poll_interval=1
    )
    assert isinstance(result, Failure) and result.reason == "observation"
    assert "wrong-id" not in repr(result)


def test_global_reassignment_and_history_converge_in_mixed_stack(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    first = global_task("task-a", "node-a")
    wrong = global_task("task-b", "node-a")
    replacement = global_task("task-c", "node-b")
    history = {**wrong, "DesiredState": "shutdown"}
    store(
        tmp_path,
        [global_service(), service("service-b")],
        [task("task-d", "service-b")],
    )
    target = StackTarget(
        services=(
            capture_global(fake_cli, policy),
            capture(fake_cli, policy, "service-b"),
        )
    )
    data = json.loads((tmp_path / "data.json").read_text())
    data["task_sequences"] = {
        "service-a": [[first, wrong], [first, history, replacement]]
    }
    (tmp_path / "data.json").write_text(json.dumps(data))
    assert isinstance(
        wait(target, policy=policy, timeout=10, poll_interval=0.01), Success
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert (
        sum(call[:2] == ["service", "ps"] and call[-1] == "service-a" for call in calls)
        == 2
    )
    assert calls[-2:] == [
        ["service", "inspect", "service-a"],
        ["service", "inspect", "service-b"],
    ]


def test_global_final_recheck_detects_supersession_in_mixed_stack(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    first, second = global_service(), service("service-b")
    store(
        tmp_path,
        [first, second],
        [
            global_task("task-a", "node-a"),
            global_task("task-b", "node-b"),
            task("task-c", "service-b"),
        ],
    )
    target = StackTarget(
        services=(
            capture_global(fake_cli, policy),
            capture(fake_cli, policy, "service-b"),
        )
    )
    changed = {**first, "Version": {"Index": 6}}
    data = json.loads((tmp_path / "data.json").read_text())
    data["sequences"] = {"service-a": [first, first, changed]}
    (tmp_path / "data.json").write_text(json.dumps(data))
    result = wait(target, policy=policy, timeout=10, poll_interval=1)
    assert isinstance(result, Failure) and result.reason == "superseded"


def test_global_partial_recheck_retains_previous_complete_cycle_and_reaps_child(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = global_task("task-a", "node-a")
    second = global_task("task-b", "node-b")
    store(
        tmp_path,
        [global_service(), service("service-b")],
        [task("task-c", "service-b")],
    )
    target = StackTarget(
        services=(
            capture_global(fake_cli, policy),
            capture(fake_cli, policy, "service-b"),
        )
    )
    data = json.loads((tmp_path / "data.json").read_text())
    stale = global_task("task-a", "node-a")
    stale["Spec"]["ForceUpdate"] = 99
    data["task_sequences"] = {"service-a": [[], [stale], [first, second]]}
    data["hang_at"] = {"args": ["service", "inspect", "service-b"], "occurrence": 5}
    (tmp_path / "data.json").write_text(json.dumps(data))
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track)
    result = wait(target, policy=policy, timeout=10, poll_interval=0.01)
    assert isinstance(result, Deadline)
    assert result.detail == DeadlineDetail(2, missing_ready_nodes=1, stale_templates=1)
    assert (tmp_path / "started").exists()
    assert all(process.returncode is not None for process in processes)
    assert all(
        process.stdout is not None and process.stdout.closed for process in processes
    )
    assert all(
        process.stderr is not None and process.stderr.closed for process in processes
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert calls[-1] == ["service", "inspect", "service-b"]
    assert calls.count(calls[-1]) == 5


A, B, C = ("a" * 64, "b" * 64, "c" * 64)
SECRETS = (A, B, C, "web", "private-project", "private-label", "private-name")
SECRETS += ("hash-1", "hash-2", "credential-payload")
PS = ["compose", "ps", "--all", "--quiet", "web"]


def container(
    identity: str,
    *,
    service: str = "web",
    status: str = "running",
    health: str | None = None,
    config_hash: str = "hash-1",
    oneoff: bool = False,
    restarting: bool = False,
    paused: bool = False,
    exit_code: int = 0,
    restarts: int | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "Status": status,
        "Running": status in {"running", "restarting", "paused"},
        "Restarting": restarting,
        "Paused": paused,
        "ExitCode": exit_code,
    }
    if health is not None:
        state["Health"] = {"Status": health, "Log": [{"Output": "credential-payload"}]}
    wire: dict[str, Any] = {
        "Id": identity,
        "Name": "/private-name",
        "State": state,
        "Config": {
            "Env": ["TOKEN=credential-payload"],
            "Cmd": ["--password=credential-payload"],
            "Labels": {
                "com.docker.compose.project": "private-project",
                "com.docker.compose.service": service,
                "com.docker.compose.config-hash": config_hash,
                "com.docker.compose.oneoff": "True" if oneoff else "False",
                "com.example.private": "private-label",
            },
        },
    }
    if restarts is not None:
        wire["RestartCount"] = restarts
    return wire


def snapshot(
    *members: dict[str, Any], listing: list[str] | None = None
) -> dict[str, Any]:
    return {
        "listing": [item["Id"] for item in members] if listing is None else listing,
        "containers": {item["Id"]: item for item in members},
    }


def store_compose(path: Path, *snapshots: dict[str, Any]) -> None:
    """Replace the scripted snapshots and restart the per-cycle call count."""
    (path / "calls").unlink(missing_ok=True)
    (path / "data.json").write_text(
        json.dumps({"services": {}, "tasks": [], "compose": list(snapshots)})
    )


def compose_target(
    client: DockerClient,
    policy: InspectionPolicy,
    services: tuple[str, ...] = ("web",),
    *,
    completion: tuple[str, ...] = (),
) -> ComposeTarget:
    result = capture_compose(
        client, services, completion_services=completion, policy=policy, timeout=5
    )
    assert isinstance(result, ComposeTarget)
    return result


@pytest.mark.parametrize(
    "test",
    [["CMD", "credential-payload"], ["CMD-SHELL", "credential-payload"], [], ["NONE"]],
)
def test_compose_configured_healthcheck_requires_health_state(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    test: list[str],
) -> None:
    member = container(A)
    member["Config"]["Healthcheck"] = {"Test": test}
    store_compose(tmp_path, snapshot(member))
    target = compose_target(fake_cli, policy)
    outcome = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    if test and test[0] != "NONE":
        assert outcome == Deadline(
            DeadlineDetail(captured_services=1, unhealthy_containers=1)
        )
    else:
        assert outcome == Success()


def calls_of(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (path / "calls").read_text().splitlines()]


def inspection(identity: str) -> list[str]:
    return ["inspect", "--type", "container", identity]


def listings(path: Path) -> int:
    return sum(call[0] == "compose" for call in calls_of(path))


def track_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[subprocess.Popen[bytes]]:
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        process = popen(args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track)
    return processes


def assert_reaped(processes: list[subprocess.Popen[bytes]]) -> None:
    assert processes
    for process in processes:
        assert process.returncode is not None
        assert process.stdout is not None and process.stdout.closed
        assert process.stderr is not None and process.stderr.closed


def hang_at(path: Path, args: list[str], occurrence: int) -> None:
    data = json.loads((path / "data.json").read_text())
    data["hang_at"] = {"args": args, "occurrence": occurrence}
    (path / "data.json").write_text(json.dumps(data))


def test_compose_capture_stores_full_ids_and_per_service_hashes(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(
            container(A), container(B), container(C, service="db", config_hash="hash-2")
        ),
    )
    target = compose_target(fake_cli, policy, ("web", "db"))
    assert target.services == (
        ComposeService(name="web", config_hash="hash-1", container_ids=(A, B)),
        ComposeService(name="db", config_hash="hash-2", container_ids=(C,)),
    )
    assert calls_of(tmp_path)[0] == [*PS, "db"]


def test_compose_listing_follows_configured_project_globals(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    fake_cli.client_config.compose_project_name = "demo"
    store_compose(tmp_path, snapshot(container(A)))
    compose_target(fake_cli, policy)
    assert calls_of(tmp_path)[0] == [
        "compose",
        "--project-name",
        "demo",
        "ps",
        "--all",
        "--quiet",
        "web",
    ]


def test_compose_healthy_and_unchecked_services_succeed_after_a_final_relist(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(container(A, health="healthy"), container(B, service="db")),
    )
    target = compose_target(fake_cli, policy, ("web", "db"))
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()
    listing = [*PS, "db"]
    assert calls_of(tmp_path) == [listing, inspection(A), inspection(B)] * 3


@pytest.mark.parametrize(
    "blockers",
    [
        [{"health": "starting"}, {"health": "unhealthy"}],
        [{"status": "restarting", "restarting": True}, {"status": "created"}],
        [{"status": "paused", "paused": True}],
    ],
    ids=["health", "restart", "pause"],
)
def test_compose_keeps_polling_through_blockers_until_ready(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    blockers: list[dict[str, Any]],
) -> None:
    stages: list[dict[str, Any]] = [
        {"health": "healthy"},
        *blockers,
        {"health": "healthy"},
    ]
    store_compose(tmp_path, *(snapshot(container(A, **stage)) for stage in stages))
    target = compose_target(fake_cli, policy)
    assert wait(target, policy=policy, timeout=10, poll_interval=0.01) == Success()
    assert listings(tmp_path) == len(stages) + 1


def test_compose_success_needs_the_final_pass_to_be_ready_too(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    ready, unhealthy = container(A, health="healthy"), container(A, health="unhealthy")
    store_compose(
        tmp_path, snapshot(ready), snapshot(ready), snapshot(unhealthy), snapshot(ready)
    )
    target = compose_target(fake_cli, policy)
    assert wait(target, policy=policy, timeout=10, poll_interval=0.01) == Success()
    assert listings(tmp_path) == 5


def test_compose_final_pass_detects_change_after_all_are_ready(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(container(A)),
        snapshot(container(A)),
        snapshot(container(A), container(B)),
    )
    target = compose_target(fake_cli, policy)
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="superseded")
    assert listings(tmp_path) == 3


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({"restarting": True}, DeadlineDetail(1, restarting_containers=1)),
        ({"paused": True}, DeadlineDetail(1, paused_containers=1)),
        ({"status": "exited"}, DeadlineDetail(1, non_running_containers=1)),
        ({"health": "starting"}, DeadlineDetail(1, unhealthy_containers=1)),
        (
            {"status": "restarting", "restarting": True, "health": "unhealthy"},
            DeadlineDetail(
                1,
                non_running_containers=1,
                restarting_containers=1,
                unhealthy_containers=1,
            ),
        ),
    ],
    ids=["restarting-flag", "paused-flag", "exited", "starting", "overlap"],
)
def test_compose_non_ready_states_poll_until_the_deadline(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    flags: dict[str, Any],
    expected: DeadlineDetail,
) -> None:
    store_compose(tmp_path, snapshot(container(A, **flags)))
    target = compose_target(fake_cli, policy)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(expected)


def test_compose_counts_each_affected_service_once_across_a_mixed_project(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    names = ("web", "db", "cache", "queue", "ok")
    store_compose(
        tmp_path,
        snapshot(
            container(A, restarting=True),
            container(B, service="db", paused=True, status="paused"),
            container(C, service="cache", status="exited"),
            container("d" * 64, service="cache", status="dead"),
            container("e" * 64, service="queue", health="starting"),
            container("f" * 64, service="ok", health="healthy"),
        ),
    )
    target = compose_target(fake_cli, policy, names)
    result = wait(target, policy=policy, timeout=2, poll_interval=0.01)
    assert result == Deadline(
        DeadlineDetail(
            5,
            non_running_containers=2,
            restarting_containers=1,
            paused_containers=1,
            unhealthy_containers=1,
        )
    )


def test_exited_zero_one_shot_is_a_blocker_unless_left_out_of_the_selection(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(container(A), container(B, service="migrate", status="exited")),
    )
    with_one_shot = compose_target(fake_cli, policy, ("web", "migrate"))
    assert with_one_shot.services[1].container_ids == (B,)
    blocked = wait(with_one_shot, policy=policy, timeout=1.5, poll_interval=0.01)
    assert blocked == Deadline(DeadlineDetail(2, non_running_containers=1))
    long_running = compose_target(fake_cli, policy)
    assert wait(long_running, policy=policy, timeout=5, poll_interval=0.01) == Success()


MIGRATE = ("web", "migrate")


def migrating(
    *,
    status: str = "running",
    health: str | None = None,
    restarting: bool = False,
    paused: bool = False,
    exit_code: int = 0,
) -> dict[str, Any]:
    return snapshot(
        container(A),
        container(
            B,
            service="migrate",
            status=status,
            health=health,
            restarting=restarting,
            paused=paused,
            exit_code=exit_code,
        ),
    )


def test_compose_completion_service_is_marked_at_capture(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, migrating(status="exited"))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    assert [service.completion for service in target.services] == [False, True]
    plain = compose_target(fake_cli, policy, MIGRATE)
    assert [service.completion for service in plain.services] == [False, False]
    assert target != plain


def test_compose_mixed_selection_succeeds_when_completion_container_exited_zero(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, migrating(status="exited"))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()
    listing = [*PS, "migrate"]
    assert calls_of(tmp_path) == [listing, inspection(A), inspection(B)] * 3


def test_compose_completion_health_is_ignored_once_exited_zero(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, migrating(status="exited", health="unhealthy"))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()


@pytest.mark.parametrize("exit_code", [1, 137])
def test_compose_completion_nonzero_exit_fails_immediately_without_payload(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, exit_code: int
) -> None:
    store_compose(tmp_path, migrating(status="exited", exit_code=exit_code))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    started = time.monotonic()
    result = wait(target, policy=policy, timeout=30, poll_interval=5)
    assert time.monotonic() - started < 5
    assert result == Failure(reason="completion_failed")
    assert isinstance(result, Failure)
    assert (result.service_id, result.state, result.cause) == (None, None, None)
    assert str(exit_code) not in repr(result)
    assert not any(secret in repr(result) for secret in SECRETS)
    assert listings(tmp_path) == 2


def test_compose_nonzero_exit_of_a_long_running_service_is_not_a_completion_failure(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(container(A, status="exited", exit_code=1)))
    target = compose_target(fake_cli, policy)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(DeadlineDetail(1, non_running_containers=1))


@pytest.mark.parametrize(
    "flags",
    [
        {"status": "created"},
        {"status": "running"},
        {"status": "paused", "paused": True},
        {"status": "dead", "exit_code": 1},
        {"status": "restarting", "restarting": True, "exit_code": 1},
        {"status": "exited", "restarting": True, "exit_code": 1},
        {"status": "exited", "restarting": True},
    ],
    ids=[
        "created",
        "running",
        "paused",
        "dead",
        "restarting",
        "exited-restarting-nonzero",
        "exited-restarting-zero",
    ],
)
def test_compose_unfinished_completion_container_is_counted_not_failed(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    flags: dict[str, Any],
) -> None:
    store_compose(tmp_path, migrating(**flags))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(DeadlineDetail(2, unfinished_completion_containers=1))


def test_compose_unfinished_counter_is_per_service_and_joins_other_blockers(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(
            container(A, health="starting"),
            container(B, service="migrate", status="created"),
            container(C, service="migrate", status="exited"),
            container("d" * 64, service="seed", status="exited"),
        ),
    )
    names = ("web", "migrate", "seed")
    target = compose_target(fake_cli, policy, names, completion=("migrate", "seed"))
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(
        DeadlineDetail(3, unhealthy_containers=1, unfinished_completion_containers=1)
    )


def test_compose_polls_until_the_completion_container_finishes(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    stages = [
        migrating(status="created"),
        migrating(status="running"),
        migrating(status="restarting", restarting=True, exit_code=1),
        migrating(status="exited"),
    ]
    store_compose(tmp_path, *stages)
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    assert wait(target, policy=policy, timeout=10, poll_interval=0.01) == Success()
    assert listings(tmp_path) == len(stages) + 1


def test_compose_completion_failure_is_not_reached_while_restarting(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        migrating(status="created"),
        migrating(status="restarting", restarting=True, exit_code=1),
        migrating(status="exited", exit_code=1),
    )
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=10, poll_interval=0.01)
    assert result == Failure(reason="completion_failed")
    assert listings(tmp_path) == 3


def test_compose_gone_completion_container_is_superseded_not_failed(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, migrating(status="exited", exit_code=1))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    store_compose(
        tmp_path, migrating(status="exited"), snapshot(container(A), listing=[A, B])
    )
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="superseded")


def test_compose_completion_service_without_containers_is_unsupported(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(container(A)))
    result = capture_compose(
        fake_cli,
        MIGRATE,
        completion_services=("migrate",),
        policy=policy,
        timeout=5,
    )
    assert result == Failure(reason="unsupported")


@pytest.mark.parametrize(
    "final,expected",
    [
        (migrating(status="exited", exit_code=2), Failure(reason="completion_failed")),
        (migrating(status="created"), Success()),
        (
            migrating(status="restarting", restarting=True, exit_code=1),
            Success(),
        ),
    ],
    ids=["nonzero", "created", "restarting"],
)
def test_compose_completion_success_needs_the_final_pass_to_finish_too(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    final: dict[str, Any],
    expected: Failure | Success,
) -> None:
    done = migrating(status="exited")
    store_compose(tmp_path, done, done, final, done, done)
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=10, poll_interval=0.01)
    assert result == expected
    assert listings(tmp_path) == (3 if isinstance(expected, Failure) else 5)


def test_compose_final_pass_detects_completion_change_after_all_are_done(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    done = migrating(status="exited")
    store_compose(tmp_path, done, done, snapshot(container(A)))
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="superseded")
    assert listings(tmp_path) == 3


@pytest.mark.parametrize("exit_code", [None, "0", False, 0.0])
def test_compose_completion_without_a_usable_exit_code_is_malformed(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, exit_code: object
) -> None:
    done = migrating(status="exited")
    wire = container(B, service="migrate", status="exited")
    if exit_code is None:
        del wire["State"]["ExitCode"]
    else:
        wire["State"]["ExitCode"] = exit_code
    damaged = snapshot(container(A), wire)
    damaged["raw"] = {B: json.dumps([wire])}
    store_compose(tmp_path, done, done, damaged)
    target = compose_target(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="malformed")
    assert isinstance(result, Failure) and result.cause is None


@pytest.mark.parametrize(
    "completion",
    [("db",), ("web", "web"), ["web"], "web", (1,), ("",)],
    ids=["unselected", "duplicate", "list", "string", "not-string", "blank"],
)
def test_compose_capture_rejects_invalid_completion_before_launch(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    completion: tuple[str, ...],
) -> None:
    store_compose(tmp_path, snapshot(container(A)))
    with pytest.raises(ValueError):
        capture_compose(
            fake_cli,
            ("web",),
            completion_services=completion,
            policy=policy,
            timeout=5,
        )
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize(
    "changed",
    [
        snapshot(container(A), container(C)),
        snapshot(container(A), container(B, config_hash="hash-2")),
        snapshot(container(A), container(B), container(C)),
        snapshot(container(A)),
        snapshot(),
        snapshot(container(A), listing=[A, B]),
        snapshot(container(A), container(B, service="db")),
    ],
    ids=[
        "recreated",
        "config-hash",
        "scaled-up",
        "vanished",
        "all-vanished",
        "vanished-after-listing",
        "relabelled",
    ],
)
def test_compose_membership_or_hash_change_is_superseded(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    changed: dict[str, Any],
) -> None:
    store_compose(tmp_path, snapshot(container(A), container(B)), changed)
    target = compose_target(fake_cli, policy)
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="superseded")
    assert "web" not in repr(result) and A not in repr(result)


def test_compose_ignores_one_off_containers_in_capture_and_every_cycle(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    web = container(A)
    one_off = container(C, oneoff=True, status="exited", health="unhealthy")
    store_compose(
        tmp_path, snapshot(web, one_off), snapshot(web), snapshot(web, one_off)
    )
    target = compose_target(fake_cli, policy)
    assert target.services[0].container_ids == (A,)
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()
    assert inspection(C) in calls_of(tmp_path)


@pytest.mark.parametrize(
    "members,services",
    [
        ([], ("web",)),
        ([container(A)], ("web", "db")),
        ([container(A, oneoff=True)], ("web",)),
        ([container(A), container(B, config_hash="hash-2")], ("web",)),
    ],
    ids=["none", "missing-service", "only-one-off", "mixed-hashes"],
)
def test_compose_capture_rejects_unusable_selection(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    members: list[dict[str, Any]],
    services: tuple[str, ...],
) -> None:
    store_compose(tmp_path, snapshot(*members))
    result = capture_compose(fake_cli, services, policy=policy, timeout=5)
    assert result == Failure(reason="unsupported")


def broken(identity: str, kind: str) -> str:
    wire = container(identity, health="healthy")
    if kind == "not-json":
        return "not json"
    if kind == "no-results":
        return "[]"
    if kind == "two-results":
        return json.dumps([wire, container(C)])
    if kind == "short-id":
        wire["Id"] = identity[:12]
    elif kind == "other-id":
        wire["Id"] = C
    elif kind == "no-state":
        del wire["State"]
    elif kind == "no-exit-code":
        del wire["State"]["ExitCode"]
    elif kind == "string-exit-code":
        wire["State"]["ExitCode"] = "0"
    elif kind == "bool-exit-code":
        wire["State"]["ExitCode"] = False
    elif kind == "string-flag":
        wire["State"]["Paused"] = "false"
    elif kind == "empty-status":
        wire["State"]["Status"] = ""
    elif kind == "numeric-health":
        wire["State"]["Health"]["Status"] = 3
    elif kind == "null-labels":
        wire["Config"]["Labels"] = None
    elif kind == "no-service-label":
        del wire["Config"]["Labels"]["com.docker.compose.service"]
    return json.dumps([wire])


def corrupted(kind: str) -> dict[str, Any]:
    damaged = snapshot(container(A, health="healthy"))
    if kind == "duplicate-listing":
        damaged["raw_listing"] = f"{A}\n{A}"
    elif kind == "blank-listing":
        damaged["raw_listing"] = f"{A}\n\n"
    else:
        damaged["raw"] = {A: broken(A, kind)}
    return damaged


MALFORMED = [
    "not-json",
    "no-results",
    "two-results",
    "short-id",
    "other-id",
    "no-state",
    "no-exit-code",
    "string-exit-code",
    "bool-exit-code",
    "string-flag",
    "empty-status",
    "numeric-health",
    "null-labels",
    "no-service-label",
    "duplicate-listing",
    "blank-listing",
]


@pytest.mark.parametrize("kind", MALFORMED)
def test_compose_malformed_output_fails_without_payload_in_capture_and_wait(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, kind: str
) -> None:
    store_compose(tmp_path, corrupted(kind))
    captured = capture_compose(fake_cli, ("web",), policy=policy, timeout=5)
    assert isinstance(captured, Failure) and captured.reason == "malformed"
    store_compose(tmp_path, snapshot(container(A, health="healthy")), corrupted(kind))
    result = wait(
        compose_target(fake_cli, policy), policy=policy, timeout=5, poll_interval=0.01
    )
    assert isinstance(result, Failure) and result.reason == "malformed"
    assert captured.cause is None and result.cause is None
    assert not any(secret in repr(captured) + repr(result) for secret in SECRETS)


def test_compose_capture_without_config_hash_label_is_malformed(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    unlabelled = container(A)
    del unlabelled["Config"]["Labels"]["com.docker.compose.config-hash"]
    store_compose(tmp_path, snapshot(unlabelled))
    result = capture_compose(fake_cli, ("web",), policy=policy, timeout=5)
    assert isinstance(result, Failure) and result.reason == "malformed"


def test_compose_capture_rejects_a_listing_outside_the_selection(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    unselected = snapshot(container(A), container(B, service="db"))
    unselected["raw_listing"] = f"{A}\n{B}"
    store_compose(tmp_path, unselected)
    result = capture_compose(fake_cli, ("web",), policy=policy, timeout=5)
    assert result == Failure(reason="malformed")


@pytest.mark.parametrize(
    "services",
    [(), ("web", "web"), ("",), (1,), ["web"], "web"],
    ids=["empty", "duplicate", "blank", "not-text", "list", "text"],
)
def test_compose_capture_rejects_invalid_selection_before_launch(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, services: object
) -> None:
    selection: Any = services
    with pytest.raises(ValueError):
        capture_compose(fake_cli, selection, policy=policy, timeout=5)
    assert not (tmp_path / "calls").exists()


def test_compose_dry_run_client_is_rejected_at_capture_and_wait(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(container(A)))
    target = compose_target(fake_cli, policy)
    launched = len(calls_of(tmp_path))
    fake_cli.client_config.compose_dry_run = True
    with pytest.raises(ValueError):
        capture_compose(fake_cli, ("web",), policy=policy, timeout=5)
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="unsupported")
    assert len(calls_of(tmp_path)) == launched


@pytest.mark.parametrize("command", ["listing", "inspect"])
def test_compose_cancellation_reaps_hung_child(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    store_compose(tmp_path, snapshot(container(A)))
    target = compose_target(fake_cli, policy)
    hung = PS if command == "listing" else inspection(A)
    hang_at(tmp_path, hung, 2)
    processes = track_processes(monkeypatch)
    cancel = Event()
    results: list[object] = []
    worker = Thread(
        target=lambda: results.append(
            wait(target, policy=policy, timeout=30, poll_interval=1, cancel=cancel)
        )
    )
    worker.start()
    end = time.monotonic() + 10
    while not (tmp_path / "started").exists() and time.monotonic() < end:
        Event().wait(0.01)
    assert (tmp_path / "started").exists()
    cancel.set()
    worker.join(5)
    assert not worker.is_alive()
    assert results == [Cancelled()]
    assert_reaped(processes)
    assert calls_of(tmp_path)[-1] == hung


def test_compose_cancel_is_noticed_within_sleep_slices(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywhales.convergence._compose as compose_module

    store_compose(tmp_path, snapshot(container(A, health="starting")))
    target = compose_target(fake_cli, policy)
    cancel = Event()
    slices: list[float] = []

    def sleep(seconds: float) -> None:
        slices.append(seconds)
        cancel.set()

    clock = SimpleNamespace(monotonic=time.monotonic, sleep=sleep)
    monkeypatch.setattr(compose_module, "time", clock)
    result = wait(target, policy=policy, timeout=30, poll_interval=20, cancel=cancel)
    assert result == Cancelled()
    assert slices == [0.05]


@pytest.mark.parametrize(
    "health,hung,occurrence,expected",
    [
        ("starting", "listing", 2, None),
        ("starting", "inspect", 3, DeadlineDetail(1, unhealthy_containers=1)),
        ("healthy", "listing", 3, None),
    ],
    ids=["first-listing", "second-inspect", "final-pass"],
)
def test_compose_deadline_reaps_hung_child_and_keeps_last_complete_cycle(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health: str,
    hung: str,
    occurrence: int,
    expected: DeadlineDetail | None,
) -> None:
    store_compose(tmp_path, snapshot(container(A, health=health)))
    target = compose_target(fake_cli, policy)
    command = PS if hung == "listing" else inspection(A)
    hang_at(tmp_path, command, occurrence)
    processes = track_processes(monkeypatch)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(expected)
    assert (tmp_path / "started").exists()
    assert_reaped(processes)
    assert calls_of(tmp_path)[-1] == command
    assert calls_of(tmp_path).count(command) == occurrence


def test_compose_every_launch_runs_under_the_observation_scope(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywhales.convergence._wire as wire_module
    from pywhales._observation import get_observation

    cancel = Event()
    scoped: list[bool] = []
    launch = wire_module.run

    def spy(args: Sequence[object]) -> str:
        state = get_observation()
        scoped.append(
            state is not None
            and state.policy == policy
            and state.deadline is not None
            and cancel in state.cancel_tokens
        )
        return launch(args)

    monkeypatch.setattr(wire_module, "run", spy)
    store_compose(tmp_path, snapshot(container(A)))
    captured = capture_compose(
        fake_cli, ("web",), policy=policy, timeout=5, cancel=cancel
    )
    assert isinstance(captured, ComposeTarget)
    result = wait(captured, policy=policy, timeout=5, poll_interval=0.01, cancel=cancel)
    assert result == Success()
    assert len(scoped) == len(calls_of(tmp_path)) == 6 and all(scoped)


@pytest.mark.parametrize("limit", ["max_argv_bytes", "max_response_bytes"])
@pytest.mark.parametrize("stage", ["capture", "wait"])
def test_compose_observation_limits_are_failures_with_the_cause(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    limit: str,
    stage: str,
) -> None:
    from pywhales.exceptions import ObservationLimitError

    store_compose(tmp_path, snapshot(container(A)))
    bounded = replace(policy, **{limit: 1})
    if stage == "capture":
        result = capture_compose(fake_cli, ("web",), policy=bounded, timeout=5)
    else:
        target = compose_target(fake_cli, policy)
        result = wait(target, policy=bounded, timeout=5, poll_interval=0.01)
    assert isinstance(result, Failure) and result.reason == "observation"
    assert isinstance(result.cause, ObservationLimitError)


def test_compose_capture_deadline_has_no_detail(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(container(A)))
    hang_at(tmp_path, PS, 1)
    result = capture_compose(fake_cli, ("web",), policy=policy, timeout=1)
    assert result == Deadline(None)
    assert (tmp_path / "started").exists()


def test_compose_capture_cancelled_before_launch_runs_nothing(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    cancel = Event()
    cancel.set()
    result = capture_compose(
        fake_cli, ("web",), policy=policy, timeout=5, cancel=cancel
    )
    assert result == Cancelled()
    assert not (tmp_path / "calls").exists()


def test_compose_outcomes_never_carry_identities_labels_or_payloads(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
) -> None:
    ready = snapshot(container(A, health="healthy"))
    store_compose(tmp_path, ready)
    target = compose_target(fake_cli, policy)
    outcomes = [wait(target, policy=policy, timeout=5, poll_interval=0.01)]
    store_compose(tmp_path, ready, snapshot(container(C)))
    outcomes.append(wait(target, policy=policy, timeout=5, poll_interval=0.01))
    store_compose(tmp_path, ready, snapshot(container(A, health="unhealthy")))
    outcomes.append(wait(target, policy=policy, timeout=1.5, poll_interval=0.01))
    store_compose(tmp_path, ready, corrupted("not-json"))
    outcomes.append(wait(target, policy=policy, timeout=5, poll_interval=0.01))
    assert [type(outcome) for outcome in outcomes] == [
        Success,
        Failure,
        Deadline,
        Failure,
    ]
    for outcome in outcomes:
        shown = (
            repr(outcome)
            + str(outcome)
            + str(vars(outcome) if hasattr(outcome, "__dict__") else "")
        )
        assert not any(secret in shown for secret in SECRETS)
    assert not any(
        isinstance(getattr(outcome, "detail", None), Mapping) for outcome in outcomes
    )


def test_compose_diagnostics_withhold_payloads_when_debugging(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PYWHALES_DEBUG", "1")
    caplog.set_level(logging.DEBUG)
    store_compose(tmp_path, snapshot(container(A, health="healthy")))
    target = compose_target(fake_cli, policy)
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()
    output = capfd.readouterr()
    shown = output.out + output.err + caplog.text
    assert not any(secret in shown for secret in ("credential-payload", A, "private"))


WEB_SERVICE = ComposeService(name="web", config_hash="hash-1", container_ids=(A,))


@pytest.mark.parametrize(
    "fields_",
    [
        {"name": "web", "config_hash": "hash-1", "container_ids": ()},
        {"name": "web", "config_hash": "hash-1", "container_ids": ("",)},
        {"name": "web", "config_hash": "hash-1", "container_ids": (A, A)},
        {"name": "web", "config_hash": "hash-1", "container_ids": [A]},
        {"name": "", "config_hash": "hash-1", "container_ids": (A,)},
        {"name": "web", "config_hash": "", "container_ids": (A,)},
        {
            "name": "web",
            "config_hash": "hash-1",
            "container_ids": (A,),
            "completion": 1,
        },
    ],
    ids=[
        "empty",
        "blank-id",
        "duplicate-id",
        "list",
        "no-name",
        "no-hash",
        "non-bool-completion",
    ],
)
def test_compose_service_rejects_invalid_identity(fields_: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ComposeService(**fields_)


@pytest.mark.parametrize(
    "services",
    [
        (),
        [WEB_SERVICE],
        (WEB_SERVICE, WEB_SERVICE),
        (
            WEB_SERVICE,
            ComposeService(name="db", config_hash="hash-1", container_ids=(A,)),
        ),
        ("web",),
    ],
    ids=["empty", "list", "duplicate-name", "duplicate-id", "not-service"],
)
def test_compose_target_rejects_invalid_membership(
    fake_cli: DockerClient, services: object
) -> None:
    members: Any = services
    with pytest.raises(ValueError):
        ComposeTarget(client_config=fake_cli.client_config, services=members)


def test_compose_target_is_frozen_and_keeps_config_out_of_equality_and_repr(
    fake_cli: DockerClient,
) -> None:
    other = DockerClient(client_call=["private-docker"])
    first = ComposeTarget(client_config=fake_cli.client_config, services=(WEB_SERVICE,))
    second = ComposeTarget(client_config=other.client_config, services=(WEB_SERVICE,))
    assert first == second
    assert "client_config" not in repr(second) and "private-docker" not in repr(second)
    for attribute in fields(first):
        with pytest.raises(FrozenInstanceError):
            setattr(first, attribute.name, None)
    for attribute in fields(WEB_SERVICE):
        with pytest.raises(FrozenInstanceError):
            setattr(WEB_SERVICE, attribute.name, None)


ABSENT = "<absent>"
INVALID_RESTARTS = [ABSENT, None, "2", True, 1.5, -1]
INVALID_IDS = ["absent", "null", "string", "bool", "float", "negative"]


def counted(identity: str, value: object, *, service: str = "web") -> dict[str, Any]:
    """A healthy container whose raw RestartCount is ``value`` (or missing)."""
    wire = container(identity, service=service, health="healthy")
    if value != ABSENT:
        wire["RestartCount"] = value
    return wire


def budgeted(
    client: DockerClient,
    policy: InspectionPolicy,
    services: tuple[str, ...] = ("web",),
    *,
    completion: tuple[str, ...] = (),
    budget: int = 1,
) -> ComposeTarget:
    result = capture_compose(
        client,
        services,
        completion_services=completion,
        max_restarts=budget,
        policy=policy,
        timeout=5,
    )
    assert isinstance(result, ComposeTarget)
    return result


@pytest.mark.parametrize("budget", [-1, True, False, 1.5, "1", [1]])
def test_compose_capture_rejects_invalid_restart_budget_before_launch(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, budget: object
) -> None:
    store_compose(tmp_path, snapshot(counted(A, 0)))
    invalid: Any = budget
    with pytest.raises(ValueError):
        capture_compose(
            fake_cli, ("web",), max_restarts=invalid, policy=policy, timeout=5
        )
    assert not (tmp_path / "calls").exists()


def test_compose_restart_budget_is_frozen_on_the_target_and_unset_by_default(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(counted(A, 0)))
    plain = compose_target(fake_cli, policy)
    zero = budgeted(fake_cli, policy, budget=0)
    assert plain.max_restarts is None and zero.max_restarts == 0
    assert plain != zero and zero == budgeted(fake_cli, policy, budget=0)
    assert zero != budgeted(fake_cli, policy, budget=1)
    for invalid in (-1, True, 1.5, "1"):
        bad: Any = invalid
        with pytest.raises(ValueError):
            replace(zero, max_restarts=bad)


def test_compose_restart_budget_holds_a_healthy_service_until_within_budget(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    def stage(restarts: int) -> dict[str, Any]:
        return snapshot(counted(A, restarts))

    store_compose(tmp_path, stage(0), stage(0), stage(2), stage(1), stage(0))
    target = budgeted(fake_cli, policy)
    assert wait(target, policy=policy, timeout=10, poll_interval=0.01) == Success()
    assert listings(tmp_path) == 5


def test_compose_restart_budget_final_pass_must_stay_within_budget(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    ok, over = snapshot(counted(A, 1)), snapshot(counted(A, 2))
    store_compose(tmp_path, ok, ok, over, ok, ok)
    target = budgeted(fake_cli, policy)
    assert wait(target, policy=policy, timeout=10, poll_interval=0.01) == Success()
    assert listings(tmp_path) == 5


def test_compose_over_budget_service_expires_with_a_bounded_live_counter(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(tmp_path, snapshot(counted(A, 1)), snapshot(counted(A, 2)))
    target = budgeted(fake_cli, policy)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(DeadlineDetail(1, restart_budget_exceeded_containers=1))
    assert not any(secret in repr(result) for secret in SECRETS)


def test_compose_restart_counter_counts_services_once_and_joins_other_blockers(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(
            counted(A, 3),
            counted(B, 5),
            counted(C, 2, service="db"),
            container("d" * 64, service="cache", health="starting", restarts=3),
        ),
    )
    names = ("web", "db", "cache")
    target = budgeted(fake_cli, policy, names, budget=2)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(
        DeadlineDetail(3, unhealthy_containers=1, restart_budget_exceeded_containers=2)
    )


@pytest.mark.parametrize("value", [ABSENT, "x", None, 99, -1])
def test_compose_completion_services_ignore_the_restart_budget(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, value: object
) -> None:
    done = container(B, service="migrate", status="exited")
    if value != ABSENT:
        done["RestartCount"] = value
    store_compose(tmp_path, snapshot(counted(A, 0), done))
    target = budgeted(fake_cli, policy, MIGRATE, completion=("migrate",), budget=0)
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()


def test_compose_restart_budget_keeps_completion_rules_and_joins_unfinished(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    def stage(status: str, exit_code: int = 0) -> dict[str, Any]:
        return snapshot(
            counted(A, 2),
            container(B, service="migrate", status=status, exit_code=exit_code),
        )

    store_compose(tmp_path, stage("created"))
    target = budgeted(fake_cli, policy, MIGRATE, completion=("migrate",))
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(
        DeadlineDetail(
            2, unfinished_completion_containers=1, restart_budget_exceeded_containers=1
        )
    )
    store_compose(tmp_path, stage("exited"), stage("exited", exit_code=1))
    failed = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert failed == Failure(reason="completion_failed")


@pytest.mark.parametrize(
    "value", [*INVALID_RESTARTS, 0, 99], ids=[*INVALID_IDS, "zero", "huge"]
)
def test_compose_disabled_budget_ignores_any_restart_count(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, value: object
) -> None:
    store_compose(tmp_path, snapshot(counted(A, value)))
    target = compose_target(fake_cli, policy)
    assert target.max_restarts is None
    assert wait(target, policy=policy, timeout=5, poll_interval=0.01) == Success()


@pytest.mark.parametrize("value", INVALID_RESTARTS, ids=INVALID_IDS)
def test_compose_enabled_budget_malformed_restart_count_fails_safely(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, value: object
) -> None:
    store_compose(tmp_path, snapshot(counted(A, value)))
    captured = capture_compose(
        fake_cli, ("web",), max_restarts=1, policy=policy, timeout=5
    )
    ready = snapshot(counted(A, 0))
    store_compose(tmp_path, ready, ready, snapshot(counted(A, value)))
    result = wait(
        budgeted(fake_cli, policy), policy=policy, timeout=5, poll_interval=0.01
    )
    for outcome in (captured, result):
        assert outcome == Failure(reason="malformed")
        assert isinstance(outcome, Failure) and outcome.cause is None
    assert listings(tmp_path) == 3


def test_compose_enabled_budget_validates_every_member_not_just_the_first(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path
) -> None:
    store_compose(
        tmp_path,
        snapshot(counted(A, 0), counted(B, 0)),
        snapshot(counted(A, 9), counted(B, "9")),
    )
    result = wait(
        budgeted(fake_cli, policy), policy=policy, timeout=5, poll_interval=0.01
    )
    assert result == Failure(reason="malformed")


@pytest.mark.parametrize("drift", ["hash", "membership"])
def test_compose_over_budget_replacement_or_hash_drift_stays_superseded(
    fake_cli: DockerClient, policy: InspectionPolicy, tmp_path: Path, drift: str
) -> None:
    store_compose(tmp_path, snapshot(counted(A, 0)))
    target = budgeted(fake_cli, policy)
    changed = counted(A, 9)
    if drift == "hash":
        changed["Config"]["Labels"]["com.docker.compose.config-hash"] = "hash-2"
        later = snapshot(changed)
    else:
        later = snapshot(changed, counted(B, 9))
    store_compose(tmp_path, snapshot(counted(A, 0)), later)
    result = wait(target, policy=policy, timeout=5, poll_interval=0.01)
    assert result == Failure(reason="superseded")


def test_compose_restart_budget_deadline_reaps_hung_child_and_keeps_last_cycle(
    fake_cli: DockerClient,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_compose(tmp_path, snapshot(counted(A, 2)))
    target = budgeted(fake_cli, policy)
    command = inspection(A)
    hang_at(tmp_path, command, 3)
    processes = track_processes(monkeypatch)
    result = wait(target, policy=policy, timeout=1.5, poll_interval=0.01)
    assert result == Deadline(DeadlineDetail(1, restart_budget_exceeded_containers=1))
    assert (tmp_path / "started").exists()
    assert_reaped(processes)
    assert calls_of(tmp_path)[-1] == command
