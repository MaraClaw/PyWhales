import json
import os
import struct
import subprocess
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from threading import Event
from time import monotonic
from typing import override
from unittest.mock import patch

import pytest

from pywhales import (
    DockerClient,
    InspectionPolicy,
    InventoryRequest,
    Network,
    ObservationCancelled,
    ObservationLimitError,
    ObservationTimeout,
    capture_inventory,
    observation,
)
from pywhales._inventory_staging import stage_resources
from pywhales.client_config import ClientConfig, ParsingError, bulk_reload
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.components.manifest.models import ManifestListInspectResult
from pywhales.components.network.models import NetworkInspectResult
from pywhales.inventory import InventoryResource


@pytest.fixture
def fake_cli(tmp_path: Path) -> ClientConfig:
    script = tmp_path / "docker.py"
    script.write_text(
        """import json
import os
import pathlib
import sys
import threading

root = pathlib.Path(__file__).parent
log = root / 'launches.jsonl'
with log.open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
with (root / 'sentinels.jsonl').open('a') as stream:
    stream.write(json.dumps([os.environ.get('PYWHALES_TEST_INHERITED'), os.environ.get('PYWHALES_TEST_CLIENT')]) + '\\n')
if (root / 'oversize').exists():
    sys.stdout.write('X' * 40)
    sys.stdout.flush()
    sys.stderr.write('Y' * 40)
    sys.stderr.flush()
    threading.Event().wait()
if sys.argv[1:3] == ['network', 'ls']:
    print('alpha-id\\nbeta-id')
elif sys.argv[1:3] == ['network', 'inspect']:
    count = len(log.read_text().splitlines())
    changed = (root / 'conflict').exists() and count > 1
    result = []
    for reference in sys.argv[3:]:
        identity = 'alpha-id' if reference in ('alias', 'alpha-id') else reference
        name = 'alias' if identity == 'alpha-id' else 'beta'
        result.append({'Id': identity, 'Name': name, 'Labels': {'version': 'changed' if changed else 'fresh'}})
    print(json.dumps(result))
elif sys.argv[1:3] == ['manifest', 'inspect']:
    if sys.argv[3] == 'broken':
        print('not json')
    else:
        print(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.docker.distribution.manifest.list.v2+json', 'manifests': [{'digest': 'sha256:' + sys.argv[3], 'platform': {'architecture': 'amd64', 'os': 'linux'}}]}))
elif sys.argv[1:3] == ['buildx', 'ls']:
    for name in ('a', 'b'):
        print(json.dumps({'Name': name, 'Driver': 'docker'}))
else:
    sys.exit(2)
"""
    )
    return ClientConfig(client_call=[sys.executable, str(script)])


@pytest.fixture
def policy() -> InspectionPolicy:
    return InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=65536
    )


def test_capture_preserves_order_and_detaches_each_occurrence(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path
) -> None:
    handle = Network(fake_cli, "alpha-id", is_immutable_id=True)
    original = NetworkInspectResult.model_validate(
        {"Id": "alpha-id", "Name": "alias", "Labels": {"version": "old"}}
    )
    handle._set_inspect_result(original)
    request = InventoryRequest(
        kind="network", references=("alias", "beta-id"), client_config=fake_cli
    )

    snapshot = capture_inventory([request, handle], policy=policy)

    assert [record.id for record in snapshot.records] == [
        "alpha-id",
        "beta-id",
        "alpha-id",
    ]
    assert all(record.kind == "network" for record in snapshot.records)
    assert snapshot.started_at.tzinfo == UTC
    assert snapshot.started_at <= snapshot.finished_at
    first, _, repeated = snapshot.records
    assert isinstance(first.payload, NetworkInspectResult)
    assert isinstance(repeated.payload, NetworkInspectResult)
    assert original.labels is not None
    assert first.payload.labels is not None
    original.labels["version"] = "cache-mutated"
    assert first.payload.labels == {"version": "fresh"}
    first.payload.labels["version"] = "record-mutated"
    assert repeated.payload.labels == {"version": "fresh"}
    assert handle._inspect_result is original
    launches = [
        json.loads(line)
        for line in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    assert launches == [
        ["network", "inspect", "alias"],
        ["network", "inspect", "beta-id"],
        ["network", "inspect", "alpha-id"],
    ]


@pytest.mark.parametrize("operation", ["capture", "bulk"])
def test_conflicting_later_chunk_preserves_original_caches(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path, operation: str
) -> None:
    (tmp_path / "conflict").touch()
    handles = [Network(fake_cli, "alpha-id", is_immutable_id=True) for _ in range(2)]
    for handle in handles:
        handle._set_inspect_result(
            NetworkInspectResult.model_validate(
                {"Id": "alpha-id", "Name": "alias", "Labels": {"version": "old"}}
            )
        )
    before = [
        (handle._inspect_result, handle._last_refreshed_time) for handle in handles
    ]

    with pytest.raises(ParsingError):
        if operation == "capture":
            capture_inventory(handles, policy=policy)
        else:
            bulk_reload(handles, policy=policy)

    for handle, (model, timestamp) in zip(handles, before, strict=True):
        assert handle._inspect_result is model
        assert handle._last_refreshed_time == timestamp
    assert len((tmp_path / "launches.jsonl").read_text().splitlines()) == 2


def test_response_limit_reaps_real_child(
    fake_cli: ClientConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "oversize").touch()
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=65536, max_response_bytes=64
    )
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def track(
        args: list[str],
        *,
        stdout: int,
        stderr: int,
        env: Mapping[str, str],
        pass_fds: Sequence[int],
    ) -> subprocess.Popen[bytes]:
        child = real_popen(
            args, stdout=stdout, stderr=stderr, env=env, pass_fds=pass_fds
        )
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", track)
    request = InventoryRequest(
        kind="network", references=("private-reference",), client_config=fake_cli
    )
    with pytest.raises(ObservationLimitError) as error:
        capture_inventory([request], policy=policy)

    assert error.value.reason == "response"
    assert error.value.limit == 64
    assert error.value.measured is not None and error.value.measured > 64
    assert "private-reference" not in str(error.value)
    assert len(children) == 1
    assert children[0].poll() is not None
    assert children[0].stdout is not None and children[0].stdout.closed
    assert children[0].stderr is not None and children[0].stderr.closed
    assert len((tmp_path / "launches.jsonl").read_text().splitlines()) == 1


def test_empty_capture_and_bulk_do_not_resolve_or_launch(
    policy: InspectionPolicy,
) -> None:
    with (
        patch("shutil.which") as lookup,
        patch("subprocess.Popen") as launch,
        patch("subprocess.run") as run,
    ):
        snapshot = capture_inventory([], policy=policy)
        bulk_reload([], policy=policy)
    assert snapshot.records == ()
    lookup.assert_not_called()
    launch.assert_not_called()
    run.assert_not_called()


def test_equal_distinct_configs_launch_separate_groups(
    fake_cli: ClientConfig, tmp_path: Path
) -> None:
    policy = InspectionPolicy(
        max_references=2, max_argv_bytes=65536, max_response_bytes=65536
    )
    other = ClientConfig(client_call=list(fake_cli.client_call))
    assert other == fake_cli and other is not fake_cli
    requests = [
        InventoryRequest(kind="network", references=(reference,), client_config=config)
        for reference, config in (("alpha-id", fake_cli), ("beta-id", other))
    ]
    snapshot = capture_inventory(requests, policy=policy)
    assert [record.id for record in snapshot.records] == ["alpha-id", "beta-id"]
    launches = [
        json.loads(line)
        for line in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    assert launches == [
        ["network", "inspect", "alpha-id"],
        ["network", "inspect", "beta-id"],
    ]


@pytest.mark.parametrize(
    "later_value", ["invalid\0value", "x" * 4194304], ids=["invalid", "oversized"]
)
def test_inventory_reuses_upfront_full_environment_after_mutation(
    fake_cli: ClientConfig,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_value: str,
) -> None:
    monkeypatch.setenv("PYWHALES_TEST_INHERITED", "inherited-before")
    supplied = {"PYWHALES_TEST_CLIENT": "client-before"}
    first = ClientConfig(client_call=list(fake_cli.client_call), environment=supplied)
    second = ClientConfig(client_call=list(fake_cli.client_call), environment=supplied)
    supplied["PYWHALES_TEST_CLIENT"] = "caller-after"
    assert first == second and first is not second
    assert isinstance(second.environment, dict)
    later_environment = second.environment
    from pywhales import _inventory_staging

    real_inspect = _inventory_staging.inspect_group
    calls = 0

    def mutate_after_first(
        resources: Sequence[InventoryResource],
    ) -> Sequence[InventoryResource]:
        nonlocal calls
        result = real_inspect(resources)
        calls += 1
        if calls == 1:
            monkeypatch.setenv("PYWHALES_TEST_INHERITED", "inherited-after")
            later_environment["PYWHALES_TEST_CLIENT"] = later_value
        return result

    monkeypatch.setattr(_inventory_staging, "inspect_group", mutate_after_first)
    handles = [
        Network(first, "alpha-id", is_immutable_id=True),
        Network(second, "beta-id", is_immutable_id=True),
    ]

    staged = stage_resources(handles, policy)

    assert [
        handle.client_config is config
        for handle, config in zip(staged, (first, second), strict=True)
    ] == [True, True]
    assert [
        json.loads(line)
        for line in (tmp_path / "sentinels.jsonl").read_text().splitlines()
    ] == [
        ["inherited-before", "client-before"],
        ["inherited-before", "client-before"],
    ]


def test_inventory_snapshots_environment_before_argument_conversion(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path
) -> None:
    config = ClientConfig(
        client_call=list(fake_cli.client_call),
        environment={"PYWHALES_TEST_CLIENT": "before"},
    )
    assert isinstance(config.environment, dict)
    environment = config.environment

    class MutatingPath(str):
        @override
        def __str__(self) -> str:
            environment["PYWHALES_TEST_CLIENT"] = "after"
            return super().__str__()

    config.client_call[1] = MutatingPath(config.client_call[1])
    request = InventoryRequest(
        kind="network", references=("alpha-id",), client_config=config
    )

    capture_inventory([request], policy=policy)

    sentinels = [
        json.loads(line)
        for line in (tmp_path / "sentinels.jsonl").read_text().splitlines()
    ]
    assert sentinels[0][1] == "before"


def test_inventory_isolates_and_freezes_child_environment(
    fake_cli: ClientConfig,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYWHALES_TEST_INHERITED", "ambient")
    fake_cli.inherit_environment = False
    fake_cli.environment = {"PYWHALES_TEST_CLIENT": "explicit"}
    from pywhales import _inventory_staging

    inspect = _inventory_staging.inspect_group

    def mutate_after_inspect(
        resources: Sequence[InventoryResource],
    ) -> Sequence[InventoryResource]:
        result = inspect(resources)
        fake_cli.inherit_environment = True
        fake_cli.environment = {"PYWHALES_TEST_CLIENT": "changed"}
        return result

    monkeypatch.setattr(_inventory_staging, "inspect_group", mutate_after_inspect)
    request = InventoryRequest(
        kind="network", references=("alpha-id", "beta-id"), client_config=fake_cli
    )
    snapshot = capture_inventory([request], policy=policy)
    assert [record.id for record in snapshot.records] == ["alpha-id", "beta-id"]
    assert [
        json.loads(line)
        for line in (tmp_path / "sentinels.jsonl").read_text().splitlines()
    ] == [[None, "explicit"], [None, "explicit"]]


@pytest.mark.parametrize("entry", [("BAD=KEY", "value"), ("VALID", "bad\0value")])
@pytest.mark.parametrize("capture", [False, True])
def test_inventory_rejects_invalid_later_environment_before_any_inspection(
    fake_cli: ClientConfig,
    policy: InspectionPolicy,
    tmp_path: Path,
    entry: tuple[str, str],
    capture: bool,
) -> None:
    first = ClientConfig(client_call=list(fake_cli.client_call), environment={})
    second = ClientConfig(client_call=list(fake_cli.client_call), environment={})
    assert isinstance(second.environment, dict)
    second.environment[entry[0]] = entry[1]
    handles = [
        Network(first, "alpha-id", is_immutable_id=True),
        Network(second, "beta-id", is_immutable_id=True),
    ]
    for handle in handles:
        handle._set_inspect_result(
            NetworkInspectResult.model_validate({"Id": handle.id, "Name": "old"})
        )
    before = [
        (handle._inspect_result, handle._last_refreshed_time) for handle in handles
    ]

    with pytest.raises(ObservationLimitError) as failure:
        if capture:
            capture_inventory(
                [
                    InventoryRequest(
                        kind="network",
                        references=(handle.id,),
                        client_config=handle.client_config,
                    )
                    for handle in handles
                ],
                policy=policy,
            )
        else:
            bulk_reload(handles, policy=policy)

    assert failure.value.reason == "environment"
    rendered = "".join(traceback.format_exception(failure.value))
    assert entry[0] not in rendered
    assert entry[1] not in rendered
    assert not (tmp_path / "launches.jsonl").exists()
    for handle, (model, timestamp) in zip(handles, before, strict=True):
        assert handle._inspect_result is model
        assert handle._last_refreshed_time == timestamp


@pytest.mark.skipif(os.name == "nt", reason="POSIX native argv and environment budget")
def test_inventory_rejects_oversized_later_environment_before_any_inspection(
    fake_cli: ClientConfig,
    policy: InspectionPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ClientConfig(client_call=list(fake_cli.client_call), environment={})
    second = ClientConfig(
        client_call=list(fake_cli.client_call),
        environment={"PYWHALES_TEST_CLIENT": "x" * 16384},
    )
    handles = [
        Network(first, "alpha-id", is_immutable_id=True),
        Network(second, "beta-id", is_immutable_id=True),
    ]
    for handle in handles:
        handle._set_inspect_result(
            NetworkInspectResult.model_validate({"Id": handle.id, "Name": "old"})
        )
    before = [
        (handle._inspect_result, handle._last_refreshed_time) for handle in handles
    ]
    native_limit = sum(
        len(os.fsencode(f"{key}={value}")) + 1 for key, value in os.environ.items()
    )
    native_limit += (len(os.environ) + 16) * struct.calcsize("P") + 4096
    real_sysconf = os.sysconf
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: native_limit if name == "SC_ARG_MAX" else real_sysconf(name),
    )

    with pytest.raises(ObservationLimitError):
        bulk_reload(handles, policy=policy)

    assert not (tmp_path / "launches.jsonl").exists()
    for handle, (model, timestamp) in zip(handles, before, strict=True):
        assert handle._inspect_result is model
        assert handle._last_refreshed_time == timestamp


def test_observation_applies_to_list_and_inspect(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path
) -> None:
    client = DockerClient(client_call=fake_cli.client_call)
    with observation(policy):
        networks = client.network.list()
        inspected = client.network.inspect([network.id for network in networks])
    assert [network.id for network in inspected] == ["alpha-id", "beta-id"]
    launches = [
        json.loads(line)
        for line in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    assert launches == [
        ["network", "ls", "--no-trunc", "--quiet"],
        ["network", "inspect", "alpha-id", "beta-id"],
    ]


@pytest.mark.parametrize("cancelled", [True, False])
def test_observation_cancellation_and_deadline_fail_without_launch(
    policy: InspectionPolicy, tmp_path: Path, cancelled: bool
) -> None:
    client = DockerClient(client_call=[sys.executable, str(tmp_path / "missing.py")])
    cancel = Event()
    if cancelled:
        cancel.set()
    error_type = ObservationCancelled if cancelled else ObservationTimeout
    deadline = None if cancelled else monotonic() - 1
    with (
        patch("subprocess.Popen") as launch,
        pytest.raises(error_type) as error,
        observation(policy, deadline=deadline, cancel=cancel),
    ):
        client.network.list()
    assert str(tmp_path) not in str(error.value)
    launch.assert_not_called()


def read_launches(root: Path) -> list[list[str]]:
    return [
        json.loads(line) for line in (root / "launches.jsonl").read_text().splitlines()
    ]


def test_manifest_lists_launch_once_per_reference_and_forward_insecure(
    fake_cli: ClientConfig, tmp_path: Path
) -> None:
    roomy = InspectionPolicy(
        max_references=8, max_argv_bytes=65536, max_response_bytes=65536
    )
    insecure = ManifestList(fake_cli, "dup", is_immutable_id=True, insecure=True)
    secure = ManifestList(fake_cli, "dup", is_immutable_id=True)
    request = InventoryRequest(
        kind="manifest_list", references=("dup", "other"), client_config=fake_cli
    )

    snapshot = capture_inventory([insecure, request, secure], policy=roomy)

    assert [record.id for record in snapshot.records] == [
        "dup",
        "dup",
        "other",
        "dup",
    ]
    assert read_launches(tmp_path) == [
        ["manifest", "inspect", "dup", "--insecure"],
        ["manifest", "inspect", "dup"],
        ["manifest", "inspect", "other"],
        ["manifest", "inspect", "dup"],
    ]
    first = snapshot.records[0].payload
    assert isinstance(first, ManifestListInspectResult)
    assert first.name == "dup"
    assert first.manifests is not None and first.manifests[0].digest == "sha256:dup"


@pytest.mark.parametrize("operation", ["capture", "bulk"])
def test_manifest_list_later_failure_preserves_original_caches(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path, operation: str
) -> None:
    other = ClientConfig(client_call=list(fake_cli.client_call))
    handles = [
        ManifestList(fake_cli, "good", is_immutable_id=True),
        ManifestList(other, "broken", is_immutable_id=True),
    ]
    for handle in handles:
        handle._set_inspect_result(ManifestListInspectResult(name=handle.reference))
    before = [(h._inspect_result, h._last_refreshed_time) for h in handles]

    with pytest.raises(ParsingError):
        if operation == "capture":
            capture_inventory(handles, policy=policy)
        else:
            bulk_reload(handles, policy=policy)

    assert [(h._inspect_result, h._last_refreshed_time) for h in handles] == before
    assert read_launches(tmp_path) == [
        ["manifest", "inspect", "good"],
        ["manifest", "inspect", "broken"],
    ]


def test_manifest_list_bulk_reload_publishes_each_distinct_handle_once(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path
) -> None:
    first = ManifestList(fake_cli, "same", is_immutable_id=True, insecure=True)
    sibling = ManifestList(fake_cli, "same", is_immutable_id=True)

    bulk_reload([first, sibling, first], policy=policy)

    assert first._inspect_result is not None and sibling._inspect_result is not None
    assert first._inspect_result is not sibling._inspect_result
    assert first._inspect_result == sibling._inspect_result
    assert first.insecure and not sibling.insecure
    assert read_launches(tmp_path) == [
        ["manifest", "inspect", "same", "--insecure"],
        ["manifest", "inspect", "same"],
    ]


@pytest.mark.parametrize("limit", [7, 8], ids=["remaining-too-small", "exact-fit"])
def test_inventory_checks_complete_plan_against_remaining_launch_allowance(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path, limit: int
) -> None:
    client = DockerClient(client_config=fake_cli)
    other = ClientConfig(client_call=list(fake_cli.client_call))
    requests = [
        InventoryRequest(
            kind="network",
            references=("alpha-id", "beta-id", "alpha-id"),
            client_config=fake_cli,
        ),
        InventoryRequest(
            kind="builder", references=("b", "a", "b"), client_config=fake_cli
        ),
        InventoryRequest(kind="builder", references=("a",), client_config=other),
        InventoryRequest(
            kind="manifest_list", references=("dup", "dup"), client_config=fake_cli
        ),
    ]
    with observation(replace(policy, max_total_launch_attempts=limit)) as state:
        client.network.list()
        if limit == 7:
            with pytest.raises(ObservationLimitError) as failure:
                capture_inventory(requests, policy=policy)
            assert (
                failure.value.reason,
                failure.value.limit,
                failure.value.measured,
            ) == ("total_launch_attempts", 7, 8)
            assert state.launch_accounts[0].consumed == 1
            assert len(read_launches(tmp_path)) == 1
            snapshot = capture_inventory([requests[0]], policy=policy)
            assert [record.id for record in snapshot.records] == [
                "alpha-id",
                "beta-id",
                "alpha-id",
            ]
            assert state.launch_accounts[0].consumed == 4
        else:
            snapshot = capture_inventory(requests, policy=policy)
            assert [record.id for record in snapshot.records] == [
                "alpha-id",
                "beta-id",
                "alpha-id",
                "b",
                "a",
                "b",
                "a",
                "dup",
                "dup",
            ]
            assert snapshot.records[0].payload is not snapshot.records[2].payload
            assert state.launch_accounts[0].consumed == 8
            assert read_launches(tmp_path) == [
                ["network", "ls", "--no-trunc", "--quiet"],
                ["network", "inspect", "alpha-id"],
                ["network", "inspect", "beta-id"],
                ["network", "inspect", "alpha-id"],
                ["buildx", "ls", "--format", "{{json . }}"],
                ["buildx", "ls", "--format", "{{json . }}"],
                ["manifest", "inspect", "dup"],
                ["manifest", "inspect", "dup"],
            ]


def test_empty_inventory_still_succeeds_after_launch_budget_is_exhausted(
    fake_cli: ClientConfig, policy: InspectionPolicy, tmp_path: Path
) -> None:
    with observation(replace(policy, max_total_launch_attempts=1)) as state:
        DockerClient(client_config=fake_cli).network.list()
        assert capture_inventory([], policy=policy).records == ()
        bulk_reload([], policy=policy)
        assert state.launch_accounts[0].consumed == 1
    assert len(read_launches(tmp_path)) == 1
