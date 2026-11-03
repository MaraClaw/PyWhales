import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from threading import Event
from unittest.mock import patch

import pytest

from pywhales import (
    Builder,
    Context,
    InspectionPolicy,
    ObservationCancelled,
    ObservationLimitError,
    observation,
)
from pywhales._observation import get_observation
from pywhales.client_config import ClientConfig, bulk_reload
from pywhales.components.manifest.cli_wrapper import ManifestList
from pywhales.inventory import InventoryResource
from pywhales.utils import run


def test_manifest_bounded_response_limit_preserves_all_caches() -> None:
    code = "import sys; print('{}' if sys.argv[3] == 'a' else 'x' * 300)"
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    handles = [ManifestList(config, name, is_immutable_id=True) for name in ("a", "b")]
    before = [(h._inspect_result, h._last_refreshed_time) for h in handles]
    policy = InspectionPolicy(
        max_references=5, max_argv_bytes=10000, max_response_bytes=100
    )
    with pytest.raises(ObservationLimitError):
        bulk_reload(handles, policy=policy)
    assert [(h._inspect_result, h._last_refreshed_time) for h in handles] == before


def _inspect_contexts(
    resources: Sequence[InventoryResource],
) -> list[InventoryResource]:
    result: list[InventoryResource] = []
    for resource in resources:
        assert isinstance(resource, Context)
        staged = Context(
            resource.client_config, resource._get_immutable_id(), is_immutable_id=True
        )
        staged._set_inspect_result(
            staged._parse_inspect_result({"Name": resource._get_immutable_id()})
        )
        result.append(staged)
    return result


def test_explicit_policy_stages_chunks_and_publishes_distinct_handles() -> None:
    config = ClientConfig(_client_call_with_path=["docker-test"])
    first = Context(config, "first", is_immutable_id=True)
    second = Context(config, "second", is_immutable_id=True)
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )

    with patch(
        "pywhales._inventory_staging.inspect_group", side_effect=_inspect_contexts
    ) as inspect:
        bulk_reload([first, second, first], policy=policy)

    assert inspect.call_count == 2
    assert first._inspect_result is not None
    assert second._inspect_result is not None


def test_ambient_and_explicit_limits_intersect() -> None:
    config = ClientConfig(_client_call_with_path=["docker-test"])
    resources = [Context(config, name, is_immutable_id=True) for name in ("a", "b")]
    outer = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )
    inner = InspectionPolicy(
        max_references=5, max_argv_bytes=20000, max_response_bytes=20000
    )
    observed: list[InspectionPolicy] = []

    def inspect(resources: Sequence[InventoryResource]) -> list[InventoryResource]:
        state = get_observation()
        assert state is not None
        observed.append(state.policy)
        return _inspect_contexts(resources)

    with (
        observation(outer),
        patch("pywhales._inventory_staging.inspect_group", side_effect=inspect),
    ):
        bulk_reload(resources, policy=inner)

    assert observed == [outer, outer]
    assert all(resource._inspect_result is not None for resource in resources)


def test_ambient_policy_alone_selects_bounded_staging() -> None:
    config = ClientConfig(_client_call_with_path=["docker-test"])
    resources = [Context(config, name, is_immutable_id=True) for name in ("a", "b")]
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )

    with (
        observation(policy),
        patch(
            "pywhales._inventory_staging.inspect_group", side_effect=_inspect_contexts
        ) as inspect,
    ):
        bulk_reload(resources)

    assert inspect.call_count == 2
    assert all(resource._inspect_result is not None for resource in resources)


def test_later_group_limit_failure_precedes_all_inspection_and_publication() -> None:
    config = ClientConfig(_client_call_with_path=["docker-test"])
    first = Context(config, "first", is_immutable_id=True)
    second = Context(
        ClientConfig(_client_call_with_path=["docker-test"]),
        "x" * 1000,
        is_immutable_id=True,
    )
    policy = InspectionPolicy(
        max_references=5, max_argv_bytes=100, max_response_bytes=10000
    )
    before = [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in (first, second)
    ]

    with (
        patch("pywhales._inventory_staging.inspect_group") as inspect,
        pytest.raises(ObservationLimitError),
    ):
        bulk_reload([first, second], policy=policy)

    inspect.assert_not_called()
    assert [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in (first, second)
    ] == before


def test_cancellation_during_preparation_preserves_all_caches() -> None:
    config = ClientConfig(_client_call_with_path=["docker-test"])
    resources = [Context(config, name, is_immutable_id=True) for name in ("a", "b")]
    policy = InspectionPolicy(
        max_references=5, max_argv_bytes=10000, max_response_bytes=10000
    )
    cancel = Event()
    before = [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in resources
    ]
    from pywhales._bulk_reload import _prepare_publication

    def prepare(
        original: InventoryResource, staged: InventoryResource
    ) -> Callable[[], None]:
        callback = _prepare_publication(original, staged)
        cancel.set()
        return callback

    with (
        pytest.raises(ObservationCancelled),
        observation(policy, cancel=cancel),
        patch(
            "pywhales._inventory_staging.inspect_group", side_effect=_inspect_contexts
        ),
        patch("pywhales._bulk_reload._prepare_publication", side_effect=prepare),
    ):
        bulk_reload(resources, policy=policy)

    assert [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in resources
    ] == before


def test_bounded_bulk_reload_runs_inspections_through_local_cli() -> None:
    code = 'import json,sys; print(json.dumps([{"Name": value} for value in sys.argv[3:]]))'
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    resources = [Context(config, name, is_immutable_id=True) for name in ("a", "b")]
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )

    bulk_reload(resources, policy=policy)

    assert [
        resource._inspect_result.name
        for resource in resources
        if resource._inspect_result
    ] == ["a", "b"]


def test_response_limit_preserves_caches_after_earlier_chunk_succeeds() -> None:
    code = 'import json,sys; print(json.dumps([{"Name": value} for value in sys.argv[3:]]))'
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    resources = [
        Context(config, name, is_immutable_id=True) for name in ("a", "b" * 200)
    ]
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=100
    )
    before = [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in resources
    ]

    with pytest.raises(ObservationLimitError):
        bulk_reload(resources, policy=policy)

    assert [
        (resource._inspect_result, resource._last_refreshed_time)
        for resource in resources
    ] == before


def _listing_config(padding: int) -> ClientConfig:
    code = f'import json; print(json.dumps({{"Name": "b", "Driver": "x" * {padding}}}))'
    return ClientConfig(_client_call_with_path=[sys.executable, "-c", code])


def test_builder_bounded_reload_runs_one_listing_without_names_in_argv() -> None:
    code = "import json,sys; print(json.dumps({'Name': 'b', 'Driver': ' '.join(sys.argv[1:])}))"
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    builders = [Builder(config, "b", is_immutable_id=True) for _ in range(3)]
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )

    bulk_reload([*builders, builders[0]], policy=policy)

    drivers = [
        builder._inspect_result.driver
        for builder in builders
        if builder._inspect_result
    ]
    assert len(drivers) == 3 and len(set(drivers)) == 1
    assert drivers[0] == "buildx ls --format {{json . }}"


def test_builder_listing_over_response_limit_preserves_caches() -> None:
    config = _listing_config(5000)
    builder = Builder(config, "b", is_immutable_id=True)
    context = Context(
        ClientConfig(_client_call_with_path=["docker-test"]), "c", is_immutable_id=True
    )
    policy = InspectionPolicy(
        max_references=5, max_argv_bytes=10000, max_response_bytes=200
    )
    before = [(r._inspect_result, r._last_refreshed_time) for r in (builder, context)]

    with pytest.raises(ObservationLimitError):
        bulk_reload([builder, context], policy=policy)

    assert [
        (r._inspect_result, r._last_refreshed_time) for r in (builder, context)
    ] == before


def test_builder_listings_count_against_total_response_budget() -> None:
    first_config = _listing_config(150)
    second_config = _listing_config(150)
    builders = [
        Builder(first_config, "b", is_immutable_id=True),
        Builder(second_config, "b", is_immutable_id=True),
    ]
    each = InspectionPolicy(
        max_references=5, max_argv_bytes=10000, max_response_bytes=400
    )
    total = InspectionPolicy(
        max_references=5,
        max_argv_bytes=10000,
        max_response_bytes=400,
        max_total_response_bytes=300,
    )

    bulk_reload(builders, policy=each)
    assert all(builder._inspect_result is not None for builder in builders)
    fresh = [
        Builder(first_config, "b", is_immutable_id=True),
        Builder(second_config, "b", is_immutable_id=True),
    ]
    with pytest.raises(ObservationLimitError):
        bulk_reload(fresh, policy=total)
    assert all(builder._inspect_result is None for builder in fresh)


def test_competing_launch_preserves_caches_after_plan_check() -> None:
    code = 'import json,sys; print(json.dumps([{"Name": value} for value in sys.argv[3:]]))'
    config = ClientConfig(_client_call_with_path=[sys.executable, "-c", code])
    handles = [Context(config, name, is_immutable_id=True) for name in ("a", "b")]
    for handle in handles:
        handle._set_inspect_result(
            handle._parse_inspect_result({"Name": handle._get_immutable_id()})
        )
    before = [
        (handle._inspect_result, handle._last_refreshed_time) for handle in handles
    ]
    policy = InspectionPolicy(
        max_references=1, max_argv_bytes=10000, max_response_bytes=10000
    )
    first_inspected, competitor_done = Event(), Event()
    from pywhales._inventory_staging import inspect_group

    def compete() -> str:
        assert first_inspected.wait(timeout=5)
        try:
            return run([sys.executable, "-c", "print('competed')"])
        finally:
            competitor_done.set()

    inspected_count = 0

    def inspect(resources: Sequence[InventoryResource]) -> Sequence[InventoryResource]:
        nonlocal inspected_count
        result = inspect_group(resources)
        inspected_count += 1
        if inspected_count == 1:
            first_inspected.set()
            assert competitor_done.wait(timeout=5)
        return result

    with observation(replace(policy, max_total_launch_attempts=2)) as state:
        with ThreadPoolExecutor(max_workers=1) as pool:
            competitor = pool.submit(copy_context().run, compete)
            try:
                with (
                    patch(
                        "pywhales._inventory_staging.inspect_group", side_effect=inspect
                    ),
                    pytest.raises(ObservationLimitError) as refused,
                ):
                    bulk_reload(handles, policy=policy)
            finally:
                first_inspected.set()
            assert competitor.result(timeout=5) == "competed"
        assert state.launch_accounts[0].consumed == 2

    assert refused.value.reason == "total_launch_attempts"
    assert inspected_count == 1
    for handle, (payload, timestamp) in zip(handles, before, strict=True):
        assert handle._inspect_result is payload
        assert handle._last_refreshed_time == timestamp
